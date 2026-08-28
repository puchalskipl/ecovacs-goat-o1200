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
        if "," not in anchor_text or not chain_parts:
            return TrackRecord(lane_id, (), True)
        x_text, y_text, *_ = anchor_text.split(",")
        try:
            anchor = MapPosition(x=int(x_text), y=int(y_text))
        except ValueError:
            return None
        shape = decode_chain_shape(anchor, chain_parts[0], step=step)
        return TrackRecord(lane_id, (shape,) if len(shape) >= 2 else (), True)

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
