"""Derive the lawn outline from mowing coverage.

The mower's track (``onMapTrack``) covers the whole lawn once a job
completes; the boundary of that coverage is the mowing area — the same shape
the official app paints for a task. The mower never broadcasts the base-map
outline itself, so tracing the coverage is the reliable way to draw it.

The algorithm rasterises track points onto a coarse grid (one cell is roughly
one cutting width), closes small gaps with a dilation pass, then walks the
boundary of the largest covered region (Moore neighbour tracing) and drops
collinear points.
"""

from __future__ import annotations

from .mower_models import MapPosition

# One grid cell in map units. Position units are ~5 mm, so 60 units ≈ 30 cm —
# about one cutting width, which closes the gaps between adjacent lanes after
# a single dilation pass.
OUTLINE_CELL_UNITS = 60
# Grow the coverage by this many cells before tracing, closing lane gaps.
OUTLINE_DILATION_CELLS = 2
# Skip outline generation below this coverage (too few lanes to mean much).
OUTLINE_MIN_POINTS = 40

_NEIGHBOURS_8 = (
    (-1, -1), (0, -1), (1, -1),
    (1, 0), (1, 1), (0, 1),
    (-1, 1), (-1, 0),
)


def outline_from_coverage(
    points: tuple[MapPosition, ...] | list[MapPosition],
    cell_units: int = OUTLINE_CELL_UNITS,
) -> tuple[MapPosition, ...]:
    """Return the boundary polygon of the covered area (map units).

    Returns an empty tuple when there is not enough coverage to outline.
    """
    if len(points) < OUTLINE_MIN_POINTS:
        return ()

    cells = {
        (point.x // cell_units, point.y // cell_units) for point in points
    }
    for _ in range(OUTLINE_DILATION_CELLS):
        cells |= {
            (cx + dx, cy + dy)
            for cx, cy in cells
            for dx, dy in _NEIGHBOURS_8
        }

    region = _largest_region(cells)
    if len(region) < 4:
        return ()

    boundary = _trace_boundary(region)
    boundary = _drop_collinear(boundary)
    half = cell_units // 2
    return tuple(
        MapPosition(x=cx * cell_units + half, y=cy * cell_units + half)
        for cx, cy in boundary
    )


def _largest_region(cells: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Return the largest 8-connected component of covered cells."""
    remaining = set(cells)
    best: set[tuple[int, int]] = set()
    while remaining:
        seed = next(iter(remaining))
        stack = [seed]
        component = {seed}
        remaining.discard(seed)
        while stack:
            cx, cy = stack.pop()
            for dx, dy in _NEIGHBOURS_8:
                neighbour = (cx + dx, cy + dy)
                if neighbour in remaining:
                    remaining.discard(neighbour)
                    component.add(neighbour)
                    stack.append(neighbour)
        if len(component) > len(best):
            best = component
    return best


def _trace_boundary(region: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """Walk the outer boundary of a cell region (Moore neighbour tracing)."""
    start = min(region, key=lambda cell: (cell[1], cell[0]))
    boundary = [start]
    # Entered the start cell moving right; begin scanning from its left.
    direction = 6  # index into _NEIGHBOURS_8 pointing left-ish
    current = start
    for _ in range(8 * len(region)):
        found = False
        for step in range(8):
            index = (direction + step) % 8
            dx, dy = _NEIGHBOURS_8[index]
            candidate = (current[0] + dx, current[1] + dy)
            if candidate in region:
                if candidate == start and len(boundary) > 2:
                    return boundary
                boundary.append(candidate)
                current = candidate
                # Turn back sharply relative to the direction just travelled.
                direction = (index + 5) % 8
                found = True
                break
        if not found:
            break  # isolated cell
    return boundary


def _drop_collinear(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Remove points lying on straight segments between their neighbours."""
    if len(points) < 3:
        return points
    result: list[tuple[int, int]] = []
    count = len(points)
    for index, point in enumerate(points):
        prev = points[index - 1]
        nxt = points[(index + 1) % count]
        cross = (point[0] - prev[0]) * (nxt[1] - prev[1]) - (
            point[1] - prev[1]
        ) * (nxt[0] - prev[0])
        if cross != 0:
            result.append(point)
    return result or points
