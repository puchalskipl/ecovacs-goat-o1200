"""Decode the mower's own map geometry (lawn outline and obstacles).

The mower stores its map as vector geometry, not as a bitmap: closed shapes
are transmitted as an anchor point plus an 8-direction chain code walked on a
square grid. The official app renders the lawn by filling the outline and
punching the obstacle shapes out of it, which is what this module prepares.

Sources (all share one coordinate frame with ``onPos`` positions and
``onMapTrack`` track points, whose origin is the charging dock):

* ``onMI`` with ``type: "-1"`` — the lawn outline, plus ``centerX``/``centerY``.
* ``onArI`` — numbered layers; layer ``3`` holds the obstacle shapes.

**Grid scale.** One chain step is ``CHAIN_STEP`` map units. That value is not
assumed: ``centerX``/``centerY`` in the ``onMI`` payload give the centre of the
outline's bounding box in map units, so the scale is derived per map as
``(centerX - anchor_x) / cell_bbox_centre_x`` and verified against the Y axis.
The constant is only the fallback for payloads that omit the centre, which
keeps the decode correct for other gardens and firmware revisions.
"""

from __future__ import annotations

from dataclasses import replace
import re
from typing import Any, NamedTuple

from .mower_models import MapPosition, MowerMapInfo

# Map units per chain-code cell. Derived from the payload when possible (see
# module docstring); this is the observed grid resolution used as a fallback.
CHAIN_STEP = 50
# Accept a derived scale only within this range; anything else means the
# payload was not what we think it is, so the fallback is safer.
MIN_DERIVED_STEP = 5
MAX_DERIVED_STEP = 500
# Chain-code digits: even digits are the cardinal directions, odd digits the
# diagonals between them, counter-clockwise from north-west. Y grows the same
# way as in the position frame (no mirroring).
CHAIN_DIRECTIONS = {
    1: (-1, 1),
    2: (0, 1),
    3: (1, 1),
    4: (1, 0),
    5: (1, -1),
    6: (0, -1),
    7: (-1, -1),
    8: (-1, 0),
}
# A digit optionally followed by "(n)", meaning the step repeats n times in
# TOTAL (not n extra times). Verified two ways on a live capture: only this
# reading closes the outline loop (gap of one cell instead of 17), and only
# it makes the payload's own centre yield the same scale on both axes.
CHAIN_TOKEN = re.compile(r"(\d)(?:\((\d+)\))?")
# onArI layer holding obstacle shapes.
OBSTACLE_LAYER = "3"
# Shapes below this many points are noise rather than a real shape.
MIN_SHAPE_POINTS = 3
# Outline provenance markers (see MowerMapInfo.outline_source).
OUTLINE_SOURCE_MOWER = "mower"
OUTLINE_SOURCE_COVERAGE = "coverage"
# Consecutive cut updates further apart than this along the lap (in cells,
# 5 m on the reference lawn) are not bridged: the mower did not drive that
# stretch between them.
TRAIL_BRIDGE_LIMIT_CELLS = 100
# A cut update this far off the lap (in cells, 60 cm) is not on it — a
# relocalisation blip, some other shape — and breaks the trail instead.
TRAIL_SNAP_LIMIT_CELLS = 12
# A run this short (in cells, 1 m) hemmed in by cut on both sides is what
# sparse sampling leaves between two updates, not edge still to cut.
SLIVER_CELLS = 20


class DecodedOutline(NamedTuple):
    """A decoded lawn outline and the grid scale it was decoded with."""

    points: tuple[MapPosition, ...]
    chain_step: int


def walk_chain_cells(chain: str) -> list[tuple[int, int]]:
    """Return the chain code's path in grid cells, starting at (0, 0)."""
    x = y = 0
    cells = [(0, 0)]
    for match in CHAIN_TOKEN.finditer(chain):
        direction = CHAIN_DIRECTIONS.get(int(match.group(1)))
        if direction is None:
            continue
        repeats = int(match.group(2)) if match.group(2) else 1
        for _ in range(repeats):
            x += direction[0]
            y += direction[1]
            cells.append((x, y))
    return cells


def derive_chain_step(
    cells: list[tuple[int, int]],
    anchor: MapPosition,
    centre_x: Any,
    centre_y: Any,
) -> int | None:
    """Return the grid scale implied by the payload's bounding-box centre.

    ``centerX``/``centerY`` locate the centre of the shape's bounding box in
    map units, so dividing by the same centre in cells yields map units per
    cell. Both axes must agree, which also rejects payloads where the fields
    mean something else.
    """
    if not isinstance(centre_x, (int, float)) or not isinstance(centre_y, (int, float)):
        return None
    if len(cells) < 2:
        return None
    xs = [cell[0] for cell in cells]
    ys = [cell[1] for cell in cells]
    mid_x = (min(xs) + max(xs)) / 2
    mid_y = (min(ys) + max(ys)) / 2

    candidates = []
    for offset, mid in ((centre_x - anchor.x, mid_x), (centre_y - anchor.y, mid_y)):
        if abs(mid) < 1:
            # This axis is (near) symmetric about the anchor: it carries no
            # scale information, so it cannot confirm or contradict.
            continue
        candidates.append(offset / mid)
    if not candidates:
        return None

    step = round(sum(candidates) / len(candidates))
    if any(abs(value - step) > 1 for value in candidates):
        return None
    if not MIN_DERIVED_STEP <= step <= MAX_DERIVED_STEP:
        return None
    return step


def decode_chain_shape(
    anchor: MapPosition, chain: str, *, step: int = CHAIN_STEP
) -> tuple[MapPosition, ...]:
    """Return the polygon described by an anchor plus an 8-direction chain code.

    ``d(n)`` repeats the step n times in total. The chain emits one point per
    cell, so long straight edges arrive as hundreds of collinear points; they
    are collapsed to their end points, which keeps the shape identical while
    making it cheap to ship and draw.
    """
    return _points_from_cells(walk_chain_cells(chain), anchor, step)


def _points_from_cells(
    cells: list[tuple[int, int]], anchor: MapPosition, step: int
) -> tuple[MapPosition, ...]:
    points = [
        MapPosition(x=anchor.x + cell_x * step, y=anchor.y + cell_y * step)
        for cell_x, cell_y in cells
    ]
    return drop_collinear(points)


def drop_collinear(points: list[MapPosition]) -> tuple[MapPosition, ...]:
    """Collapse runs of collinear points to their end points."""
    if len(points) < 3:
        return tuple(points)
    simplified = [points[0]]
    for previous, current, following in zip(points, points[1:], points[2:]):
        cross = (current.x - previous.x) * (following.y - previous.y) - (
            current.y - previous.y
        ) * (following.x - previous.x)
        if cross != 0:
            simplified.append(current)
    simplified.append(points[-1])
    return tuple(simplified)


def parse_shape_record(field: str) -> tuple[str, MapPosition, str] | None:
    """Parse a ``"<id>;<x>,<y>;<chain code>"`` record.

    Returns the id, the anchor and the raw chain code, or None when the field
    is not a shape record (or carries an empty placeholder shape).
    """
    if not isinstance(field, str) or field.count(";") < 2:
        return None
    shape_id, anchor_text, chain = field.split(";", 2)
    if "," not in anchor_text or not chain:
        return None
    x_text, y_text, *_rest = anchor_text.split(",")
    try:
        anchor = MapPosition(x=int(x_text), y=int(y_text))
    except ValueError:
        return None
    return shape_id, anchor, chain


def outline_from_map_info(decoded: Any, meta: Any = None) -> DecodedOutline:
    """Return the lawn outline from a decoded ``onMI`` blob.

    The blob is ``[["1", "s1;<seq>;<x>,<y>;<chain code>"], ["2", "..."]]``;
    entry "1" is the outline. While a job runs the mower answers with an empty
    ``"s1;0;"`` placeholder, which yields no outline so the persisted one is
    kept. ``meta`` is the surrounding payload, used to derive the grid scale.
    """
    if not isinstance(decoded, list):
        return DecodedOutline((), CHAIN_STEP)
    meta = meta if isinstance(meta, dict) else {}
    for record in decoded:
        if not isinstance(record, list) or len(record) < 2 or record[0] != "1":
            continue
        field = record[1]
        if not isinstance(field, str):
            continue
        # "s1;<seq>;<x>,<y>;<chain>" — drop the leading shape marker.
        parts = field.split(";", 1)
        if len(parts) < 2:
            continue
        parsed = parse_shape_record(parts[1])
        if parsed is None:
            continue
        _seq, anchor, chain = parsed
        cells = walk_chain_cells(chain)
        step = (
            derive_chain_step(cells, anchor, meta.get("centerX"), meta.get("centerY"))
            or CHAIN_STEP
        )
        points = _points_from_cells(cells, anchor, step)
        if len(points) >= MIN_SHAPE_POINTS:
            return DecodedOutline(points, step)
    return DecodedOutline((), CHAIN_STEP)


def obstacles_from_area_info(
    decoded: Any, *, step: int = CHAIN_STEP
) -> tuple[tuple[MapPosition, ...], ...]:
    """Return obstacle polygons from a decoded ``onArI`` blob.

    Records are ``["1", "<layer>", "<count>", "<shape>", ...]``; the obstacle
    shapes live in layer ``OBSTACLE_LAYER``. They sit on the same grid as the
    outline, so they are decoded with the scale derived for the map.
    """
    if not isinstance(decoded, list):
        return ()
    shapes: list[tuple[MapPosition, ...]] = []
    for record in decoded:
        if not isinstance(record, list) or len(record) < 4:
            continue
        if record[1] != OBSTACLE_LAYER:
            continue
        for field in record[3:]:
            parsed = parse_shape_record(field)
            if parsed is None:
                continue
            _shape_id, anchor, chain = parsed
            points = decode_chain_shape(anchor, chain, step=step)
            if len(points) >= MIN_SHAPE_POINTS:
                shapes.append(points)
    return tuple(shapes)


class TrackRecord(NamedTuple):
    """One lane record: its id, its segments, and how it was encoded."""

    lane_id: str
    segments: tuple[tuple[MapPosition, ...], ...]
    is_chain: bool
    # Where a chain-coded record starts. A cut update of a single cell has
    # no shape, yet still says where the mower is (see trail_cells).
    anchor: MapPosition | None = None


def parse_track_record(field: Any, *, step: int = CHAIN_STEP) -> TrackRecord | None:
    """Parse one ``onMapTrack`` field into a lane id and its segments.

    The mower plans a job as numbered lanes and reports **what is still left
    to cut** on each of them, re-sending a lane every couple of seconds as it
    shrinks. That is the layer the app draws as hatching over the lawn, which
    disappears piece by piece as the mower works — not a trail of where it has
    been.

    Two field shapes exist, told apart by the second token:

    * ``"1;1;<id>;x,y;x,y[;x,y;x,y...]"`` — straight lanes, coordinates in
      **pairs**: each pair is one segment, and a lane split by an obstacle
      simply carries several pairs.
    * ``"1;2;<id>;x,y;<chain code>"`` — a chain-coded shape, used for the
      border lap that follows the lawn edge.

    A field with an id but no coordinates means that lane is finished.
    Returns the record with an empty ``segments`` for a finished lane, or None
    when the field is not a lane record.
    """
    if not isinstance(field, str):
        return None
    parts = field.split(";")
    if len(parts) < 3:
        return None
    subtype, lane_id, rest = parts[1], parts[2], parts[3:]
    if not rest:
        return TrackRecord(lane_id, (), subtype == "2")

    if subtype == "2":
        anchor_text, *chain_parts = rest
        if "," not in anchor_text:
            return TrackRecord(lane_id, (), True)
        x_text, y_text, *_ = anchor_text.split(",")
        try:
            anchor = MapPosition(x=int(x_text), y=int(y_text))
        except ValueError:
            return None
        chain = chain_parts[0] if chain_parts else ""
        shape = decode_chain_shape(anchor, chain, step=step)
        return TrackRecord(
            lane_id, (shape,) if len(shape) >= 2 else (), True, anchor
        )

    points: list[MapPosition] = []
    for token in rest:
        if "," not in token:
            continue
        x_text, y_text, *_ = token.split(",")
        try:
            points.append(MapPosition(x=int(x_text), y=int(y_text)))
        except ValueError:
            continue
    # Coordinates come in pairs; an odd trailing point has no partner and is
    # dropped rather than joined to the previous segment.
    segments = tuple(
        (points[i], points[i + 1]) for i in range(0, len(points) - 1, 2)
    )
    return TrackRecord(lane_id, segments, False)


def stabilise_geometry(
    remembered: MowerMapInfo | None,
    incoming: MowerMapInfo,
    *,
    learn: bool = False,
    remapped: bool = False,
) -> tuple[MowerMapInfo, MowerMapInfo | None]:
    """Keep the map's geometry stable across stale publishes.

    Outline and obstacles are persistent map data that only the mower's own
    pushes may change. Grouped refreshes assemble their result from a snapshot
    taken seconds earlier and carry whatever geometry was current then, so
    publishing them verbatim makes the lawn flip between the new shape and the
    previous one. Both copies claim the same source, so freshness cannot be
    inferred from the payload: callers pass ``learn=True`` exactly where new
    geometry genuinely arrives (an ``onMI``/``onArI`` push, or geometry
    restored from storage at startup).

    Returns the geometry to publish and the geometry to remember.
    ``remapped`` marks a new map id, whose geometry lives in a different
    coordinate frame, so anything remembered is dropped.
    """
    if remapped:
        remembered = None
    has_geometry = bool(incoming.outline) or bool(incoming.obstacles)
    if (learn or remembered is None) and has_geometry:
        return incoming, incoming
    if remembered is None:
        return incoming, None
    return (
        replace(
            incoming,
            outline=remembered.outline,
            outline_source=remembered.outline_source,
            chain_step=remembered.chain_step,
            obstacles=remembered.obstacles or incoming.obstacles,
        ),
        remembered,
    )


def carry_forward_track(
    remembered: tuple | None,
    incoming: tuple,
    *,
    from_push: bool,
    remapped: bool,
) -> tuple[tuple, tuple | None]:
    """Decide which remaining-work layer to publish, and what to remember.

    ``incoming`` and ``remembered`` are ``(lanes, border, border_template,
    border_lap_start, border_cut, border_cut_front)`` tuples. Only an
    ``onMapTrack`` push (``from_push``) may move this layer: every other
    publish — grouped refreshes above all — was assembled from a snapshot
    taken seconds earlier and would drag the layer backwards.

    All six travel in the same push, so all six must be carried. Keeping
    only the lanes left the border blanked by every ordinary refresh, and the
    card went on drawing the last loop it happened to catch instead of the
    one still to cut; losing the template would strand compose_border without
    the lap's never-transmitted tail. The border is tri-state — ``None`` (no
    snapshot yet), ``()`` (done) or the cells left — which is why the tuple
    is wrapped: it tells "remembered as None" apart from "nothing remembered
    yet".

    Returns ``(to_publish, to_remember)``.
    """
    if remapped:
        remembered = None
    if from_push:
        return incoming, incoming
    if remembered is None:
        return incoming, None
    return remembered, remembered


def compose_border(
    template: tuple | None,
    lap_start: int | None,
    arc_segments: tuple,
    *,
    step: int,
    previous: tuple | None = None,
    origin_hint: Any | None = None,
) -> tuple[tuple, tuple | None, int | None]:
    """Compose the full remaining edge lap from a mid-job arc.

    The mower announces the lap as a CLOSED chain before driving it, then
    keeps snapshotting only the arc from the loop's fixed origin to its own
    front — the tail beyond the origin (which it cuts last, ending where it
    began) is never transmitted. Drawn alone, the arc leaves that tail
    unpainted although it is still ahead: observed live 2026-08-30, the card
    lost the whole right side of the lawn 30 s into an edge trim.

    So: remember the closed announcement as the lap ``template``; when the
    first open arc arrives, the vertex nearest its front is where the mower
    broke the loop (``lap_start``); the template arc from there back through
    the listing's end is the missing tail, appended to every published
    border. An empty arc means the mower has passed the origin — only the
    tail remains.

    Returns ``(border_segments, template_points, lap_start_index)``.
    """
    points = [point for segment in arc_segments for point in segment]

    if len(points) >= 8:
        head, tail = points[0], points[-1]
        if abs(head.x - tail.x) + abs(head.y - tail.y) <= 2 * step:
            # A closed chain is the announcement of the whole lap. The size
            # floor keeps a short blip whose ends happen to sit close together
            # from being mistaken for a lap and becoming the template.
            return arc_segments, tuple(points), None

    if template is None:
        # Restart mid-job: no announcement seen, publish the arc as-is.
        return arc_segments, None, None

    if points:
        origin = template[0]
        head = points[0]
        if abs(head.x - origin.x) + abs(head.y - origin.y) > 4 * step:
            # Not the origin arc: a stub of cells around the mower has landed
            # in the border slot (observed live 2026-09-01 — a two-point run
            # at the mower's position). Composing a ring from it anchors the
            # cut/remaining split at the wrong vertex and redraws long-done
            # boundary as pending. The real mid-job arc always starts at the
            # loop's fixed origin, so anything else is noise: keep what we
            # last published.
            if previous is not None:
                return previous, template, lap_start
            kept: tuple = ()
            if lap_start is not None:
                missing_tail = template[lap_start:]
                if len(missing_tail) >= 2:
                    kept = (missing_tail,)
            return kept, template, lap_start

    if points and lap_start is None:
        # Where did the mower break the loop? The front of the first open arc
        # is only where the mower is NOW — late snapshots put whole cut
        # stretches into the never-repainted tail (observed 2026-09-01: the
        # first arc came 1.5 min into a trim and the bottom edge stayed green
        # for the rest of the job). When the caller knows a better anchor —
        # a standalone trim always begins its lap at the dock — prefer the
        # template vertex nearest that hint, as long as the hint actually
        # lies on the loop.
        lap_start = None
        if origin_hint is not None:
            candidate = min(
                range(len(template)),
                key=lambda i: abs(template[i].x - origin_hint.x)
                + abs(template[i].y - origin_hint.y),
            )
            anchor = template[candidate]
            # The dock sits a step or two off the boundary (measured 0.85 m
            # on the reference lawn), so the tolerance is generous — the
            # guard only has to reject hints that are nowhere near the loop.
            if (
                abs(anchor.x - origin_hint.x) + abs(anchor.y - origin_hint.y)
                <= 30 * step
            ):
                lap_start = candidate
        if lap_start is None:
            front = points[-1]
            lap_start = min(
                range(len(template)),
                key=lambda i: abs(template[i].x - front.x)
                + abs(template[i].y - front.y),
            )

    if lap_start is None:
        return arc_segments, template, lap_start

    missing_tail = template[lap_start:]
    if len(missing_tail) >= 2:
        return (*arc_segments, missing_tail), template, lap_start
    return arc_segments, template, lap_start


def cut_cells_from_points(
    points: Any, *, step: int = CHAIN_STEP
) -> frozenset[tuple[int, int]]:
    """Return the dilated grid cells covered by freshly-cut edge points.

    Between border snapshots the mower streams the cells it has just cut as
    small chain updates — the signal the vendor app whitens its ring with in
    real time. Each point marks its own cell plus the 8 neighbours, so that
    membership later is a single set lookup while still catching template
    vertices that sit up to a cell off the cut line.
    """
    cells = set()
    for point in points:
        cell_x, cell_y = point.x // step, point.y // step
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cells.add((cell_x + dx, cell_y + dy))
    return frozenset(cells)


def trail_cells(
    reference: tuple,
    waypoints: list[MapPosition],
    *,
    step: int = CHAIN_STEP,
    closed: bool = False,
    limit: int = TRAIL_BRIDGE_LIMIT_CELLS,
) -> frozenset[tuple[int, int]]:
    """Return the cells of the lap driven between consecutive cut updates.

    Each cut update names a few cells, but the mower drives two to three
    times that far before the next one, so eroding by the updates alone
    leaves an uncut sliver between every two of them and the ring comes out
    dashed all round — observed live 2026-09-04 during the in-mow edge pass,
    where snapshots stand still for minutes and nothing else closes the
    gaps (measured on the 2026-09-02 trim capture: 5 cells per update, 14
    cells driven in between). The mower follows the lap without skipping,
    so the stretch between one update and the next was cut too.

    ``reference`` is the lap to walk — the announced ring (``closed``) or,
    without one, the composed remainder's segments. Every waypoint snaps to
    its nearest reference point (within ``TRAIL_SNAP_LIMIT_CELLS``; a
    waypoint further off is not on the lap and breaks the trail), and the
    reference between consecutive snaps is filled — the short way round a
    closed ring, never across separate segments, and never over more than
    ``limit`` cells: two updates that far apart came from a drive elsewhere,
    not along the edge. Cells are dilated like the updates' own, so a lap
    composed a cell off the reference is still rubbed out.
    """
    if len(waypoints) < 2 or not reference:
        return frozenset()
    dense = [
        _densify(segment, step) for segment in reference if len(segment) >= 2
    ]
    if not dense:
        return frozenset()
    ring = closed and len(dense) == 1
    snap_limit = (TRAIL_SNAP_LIMIT_CELLS * step) ** 2

    def snap(point: MapPosition) -> tuple[int, int] | None:
        best: tuple[int, int, int] | None = None
        for segment_index, points in enumerate(dense):
            for index, candidate in enumerate(points):
                distance = (candidate.x - point.x) ** 2 + (
                    candidate.y - point.y
                ) ** 2
                if distance <= snap_limit and (best is None or distance < best[0]):
                    best = (distance, segment_index, index)
        return None if best is None else (best[1], best[2])

    driven: list[MapPosition] = []
    previous = snap(waypoints[0])
    for waypoint in waypoints[1:]:
        current = snap(waypoint)
        if (
            previous is not None
            and current is not None
            and previous[0] == current[0]
        ):
            points = dense[current[0]]
            start, end = previous[1], current[1]
            if ring:
                count = len(points)
                ahead = (end - start) % count
                back = (start - end) % count
                if ahead <= back and ahead <= limit:
                    driven.extend(points[(start + k) % count] for k in range(ahead + 1))
                elif back < ahead and back <= limit:
                    driven.extend(points[(start - k) % count] for k in range(back + 1))
            else:
                low, high = sorted((start, end))
                if high - low <= limit:
                    driven.extend(points[low : high + 1])
        previous = current
    return cut_cells_from_points(driven, step=step)


def _densify(segment: tuple, step: int) -> list[MapPosition]:
    """Expand a polyline so consecutive points sit at most ``step`` apart.

    Template runs collapse long straight edges to their end points (metres
    apart), so cell-level erosion must first restore intermediate points or a
    short cut would never split a long edge.
    """
    if len(segment) < 2:
        return list(segment)
    dense: list[MapPosition] = [segment[0]]
    for start, end in zip(segment, segment[1:]):
        span = max(abs(end.x - start.x), abs(end.y - start.y))
        pieces = max(1, -(-span // step))
        for i in range(1, pieces + 1):
            dense.append(
                MapPosition(
                    x=start.x + (end.x - start.x) * i // pieces,
                    y=start.y + (end.y - start.y) * i // pieces,
                )
            )
    return dense


def border_coverage_cells(
    segments: tuple, *, step: int = CHAIN_STEP
) -> frozenset[tuple[int, int]]:
    """Return the grid cells a border's polylines pass through.

    Feeds the erosion ratchet: cells present in the previously published
    border but absent from the new one were cut in the meantime (snapshots
    only ever shrink), so they join the cut set even when no update named
    them — otherwise a ring re-announcement resurrects the slivers between
    sparse updates.
    """
    cells = set()
    for segment in segments or ():
        for point in _densify(segment, step):
            cells.add((point.x // step, point.y // step))
    return frozenset(cells)


def erode_border(
    segments: tuple, cut: frozenset[tuple[int, int]], *, step: int = CHAIN_STEP
) -> tuple:
    """Erase the already-cut cells from a composed border.

    Snapshots of the in-mow edge pass can lag the cut by minutes, and the
    composed tail can span ground long done (observed live 2026-09-02: every
    snapshot repainted the cut right side green for the rest of the job) — so
    after every composition, whatever falls in the accumulated cut cells is
    rubbed out. Surviving runs are re-simplified; runs shorter than 2 points
    are dropped, and so is a run of at most ``SLIVER_CELLS`` hemmed in by
    cut on both sides — the mower never leaves a metre of edge standing
    between two cut stretches, that is a sampling gap between updates. A
    short run at a segment's own end is kept: that end is the lap's front
    or its start, not a hole.
    """
    if not segments or not cut:
        return segments
    eroded: list[tuple[MapPosition, ...]] = []
    for segment in segments:
        run: list[MapPosition] = []
        # Whether the run began right after a cut point rather than at the
        # segment's start.
        after_cut = False
        for point in _densify(segment, step):
            if (point.x // step, point.y // step) in cut:
                if len(run) >= 2 and not (
                    after_cut and len(run) - 1 <= SLIVER_CELLS
                ):
                    eroded.append(drop_collinear(run))
                run = []
                after_cut = True
            else:
                run.append(point)
        if len(run) >= 2:
            eroded.append(drop_collinear(run))
    return tuple(eroded)
