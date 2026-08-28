"""Map geometry decoding, checked against a real O1200 capture."""

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

from custom_components.ecovacs_goat.map_geometry import (
    CHAIN_STEP,
    decode_chain_shape,
    derive_chain_step,
    obstacles_from_area_info,
    outline_from_map_info,
    walk_chain_cells,
)
from custom_components.ecovacs_goat.mower_models import MapPosition

# Leading section of the lawn outline this mower reports (onMI, type "-1"),
# with the payload's own bounding-box centre for that full outline.
REAL_ANCHOR = MapPosition(x=-24900, y=3750)
REAL_CHAIN = "56(35)56(7)5(3)4(10)54(3)34(18)54(17)34(4)32(18)312(9)"


def test_scale_comes_from_the_payload_not_a_constant() -> None:
    """centerX/centerY give the grid scale, so other gardens decode correctly.

    The mower reports the centre of the outline's bounding box in map units;
    dividing by the same centre in grid cells yields map units per cell. This
    is what keeps the decode correct for maps whose grid differs from ours.
    """
    # Full-outline figures taken from a live capture: the payload's centre and
    # the decoded cell bounding box both come from the same message.
    cells = [(0, 0), (498, 275), (0, -93)]  # bbox corners are what matter
    step = derive_chain_step(cells, REAL_ANCHOR, -12450, 8300)

    assert step == 50
    assert CHAIN_STEP == 50  # fallback matches the observed grid


def test_derive_rejects_disagreeing_or_absent_centres() -> None:
    """A centre that does not agree across axes must not set the scale."""
    cells = [(0, 0), (100, 100)]
    assert derive_chain_step(cells, REAL_ANCHOR, None, None) is None
    # X implies 50, Y implies 200 -> reject rather than pick one.
    assert derive_chain_step(cells, MapPosition(x=0, y=0), 2500, 10000) is None
    # Absurd scales are rejected too.
    assert derive_chain_step(cells, MapPosition(x=0, y=0), 500_000, 500_000) is None


def test_chain_digits_map_to_the_expected_compass_directions() -> None:
    """Even digits are cardinal, odd digits diagonal; Y is not mirrored.

    Calibrated against a live capture: with this mapping the decoded outline
    is axis-aligned like the app's, contains the whole mowed track, and puts
    the dock inside the lawn.
    """
    origin = MapPosition(x=0, y=0)
    north = decode_chain_shape(origin, "2", step=10)
    east = decode_chain_shape(origin, "4", step=10)
    south = decode_chain_shape(origin, "6", step=10)
    west = decode_chain_shape(origin, "8", step=10)

    assert (north[-1].x, north[-1].y) == (0, 10)
    assert (east[-1].x, east[-1].y) == (10, 0)
    assert (south[-1].x, south[-1].y) == (0, -10)
    assert (west[-1].x, west[-1].y) == (-10, 0)
    # Odd digits sit between their neighbours.
    north_east = decode_chain_shape(origin, "3", step=10)
    assert (north_east[-1].x, north_east[-1].y) == (10, 10)


def test_real_outline_is_axis_aligned() -> None:
    """The mower's lawn edges run along the axes, as the app draws them."""
    cells = walk_chain_cells(REAL_CHAIN)
    segments = [
        (b[0] - a[0], b[1] - a[1]) for a, b in zip(cells, cells[1:]) if a != b
    ]
    axis_aligned = sum(1 for dx, dy in segments if dx == 0 or dy == 0)

    assert axis_aligned / len(segments) > 0.9


def test_outline_ignores_mid_job_placeholder() -> None:
    """During a job the mower answers with an empty shape, not an outline."""
    assert outline_from_map_info([["1", "s1;0;"], ["2", "0"]]).points == ()
    assert outline_from_map_info(None).points == ()
    assert outline_from_map_info([["2", "1"]]).points == ()


def test_outline_falls_back_to_the_known_grid_without_a_centre() -> None:
    """Payloads without centreX/centreY still decode at the observed scale."""
    decoded = [["1", f"s1;1;{REAL_ANCHOR.x},{REAL_ANCHOR.y};3(3)1(3)7(3)5(3)"]]
    outline = outline_from_map_info(decoded)

    assert outline.chain_step == CHAIN_STEP
    assert outline.points[0] == REAL_ANCHOR


def test_obstacles_only_come_from_their_layer() -> None:
    """Obstacle shapes live in layer 3; other layers must be ignored."""
    decoded = [
        ["1", "1", "0"],
        ["1", "3", "1", "100;-4250,15400;4(2)2(2)8(2)6"],
        ["1", "6", "1", "999;-1,-1;4(2)2(2)8(2)6"],
    ]
    obstacles = obstacles_from_area_info(decoded)

    assert len(obstacles) == 1
    assert obstacles[0][0] == MapPosition(x=-4250, y=15400)


def test_obstacles_use_the_maps_derived_scale() -> None:
    """Obstacles sit on the same grid as the outline, so they share its scale."""
    # An L shape: 9 cells east, then 9 cells north (a straight line would
    # collapse to two points and be dropped as noise).
    decoded = [["1", "3", "1", "100;0,0;4(9)2(9)"]]

    default = obstacles_from_area_info(decoded)[0]
    scaled = obstacles_from_area_info(decoded, step=10)[0]

    assert max(point.x for point in default) == 9 * CHAIN_STEP
    assert max(point.x for point in scaled) == 9 * 10


def test_collinear_runs_are_collapsed() -> None:
    """Long straight edges must not ship one point per chain cell."""
    straight = decode_chain_shape(MapPosition(x=0, y=0), "4(99)")

    assert len(straight) == 2
    # "(99)" is the total number of steps, not 99 on top of the first one.
    assert straight[-1].x == 99 * CHAIN_STEP


def test_repeat_count_is_the_total_number_of_steps() -> None:
    """d(n) walks n cells; reading it as n extra cells broke closed shapes.

    On a live capture the outline only closes back onto its anchor under this
    reading (one cell short instead of seventeen), and only then does the
    payload's own centre yield the same scale from both axes.
    """
    three = decode_chain_shape(MapPosition(x=0, y=0), "4(3)", step=10)
    bare = decode_chain_shape(MapPosition(x=0, y=0), "4", step=10)

    assert three[-1].x == 30
    assert bare[-1].x == 10
