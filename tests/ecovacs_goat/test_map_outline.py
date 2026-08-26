"""Tests for the coverage-derived lawn outline."""

from pathlib import Path
import sys
import types

PACKAGE_PATH = Path(__file__).parents[2] / "custom_components" / "ecovacs_goat"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(PACKAGE_PATH.parent)]
sys.modules.setdefault("custom_components", custom_components)

ecovacs_goat = types.ModuleType("custom_components.ecovacs_goat")
ecovacs_goat.__path__ = [str(PACKAGE_PATH)]
sys.modules.setdefault("custom_components.ecovacs_goat", ecovacs_goat)

from custom_components.ecovacs_goat.map_outline import (
    OUTLINE_MIN_POINTS,
    outline_from_coverage,
)
from custom_components.ecovacs_goat.mower_models import MapPosition


def _lanes(x_start: int, x_end: int, y_start: int, y_end: int) -> list[MapPosition]:
    """Simulate mowing lanes covering a rectangle (points every ~30 cm)."""
    points = []
    for x in range(x_start, x_end + 1, 50):
        for y in range(y_start, y_end + 1, 60):
            points.append(MapPosition(x=x, y=y))
    return points


def test_outline_of_l_shaped_coverage() -> None:
    """An L-shaped mowed area yields a closed boundary around both arms."""
    coverage = _lanes(0, 400, 0, 2000) + _lanes(0, 2000, 0, 400)
    outline = outline_from_coverage(coverage)

    assert len(outline) >= 6  # an L needs at least six corners
    xs = [p.x for p in outline]
    ys = [p.y for p in outline]
    # The boundary hugs the coverage with at most the dilation margin.
    margin = 4 * 60
    assert min(xs) >= -margin and max(xs) <= 2000 + margin
    assert min(ys) >= -margin and max(ys) <= 2000 + margin
    # The far corner of the vertical arm is enclosed...
    assert any(p.x < 600 and p.y > 1700 for p in outline)
    # ...and the notch (inner corner of the L) is respected: no boundary
    # point deep inside the uncovered quadrant.
    assert not any(p.x > 1000 and p.y > 1000 for p in outline)


def test_outline_requires_enough_coverage() -> None:
    """A few points (a single short lane) produce no outline."""
    coverage = [MapPosition(x=0, y=y) for y in range(0, 500, 60)]
    assert len(coverage) < OUTLINE_MIN_POINTS
    assert outline_from_coverage(coverage) == ()


def test_outline_ignores_stray_satellite_points() -> None:
    """Isolated relocation glitches far away do not distort the outline."""
    coverage = _lanes(0, 800, 0, 800)
    coverage.append(MapPosition(x=50000, y=50000))
    outline = outline_from_coverage(coverage)
    assert outline
    assert all(p.x < 2000 and p.y < 2000 for p in outline)
