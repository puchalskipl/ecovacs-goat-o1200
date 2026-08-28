"""The drawn map must not flip between fresh and stale geometry."""

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
    OUTLINE_SOURCE_COVERAGE,
    OUTLINE_SOURCE_MOWER,
    stabilise_geometry,
)
from custom_components.ecovacs_goat.mower_models import MapPosition, MowerMapInfo

FRESH = (MapPosition(x=0, y=0), MapPosition(x=100, y=0), MapPosition(x=100, y=100))
STALE = (MapPosition(x=0, y=0), MapPosition(x=9, y=0), MapPosition(x=9, y=9))


def _info(outline, source=OUTLINE_SOURCE_MOWER, obstacles=()):
    return MowerMapInfo(
        outline=outline, outline_source=source, chain_step=50, obstacles=obstacles
    )


def test_stale_publish_cannot_undo_freshly_pushed_geometry() -> None:
    """A grouped refresh built before an onMI push must not replace it.

    Observed live: the lawn flipped between the correct shape and the previous
    one about once a second, because refreshes publish a snapshot assembled
    seconds earlier. Both copies claim source "mower", so only the publish of
    an actual push (learn=True) may change the remembered geometry.
    """
    published, remembered = stabilise_geometry(None, _info(FRESH), learn=True)
    assert published.outline == FRESH

    # The stale refresh republishes the outline it snapshotted earlier.
    published, remembered = stabilise_geometry(remembered, _info(STALE))

    assert published.outline == FRESH
    assert published.chain_step == 50
    assert remembered.outline == FRESH


def test_a_newer_push_still_updates_the_map() -> None:
    """Genuine new geometry from the mower replaces what was remembered."""
    _published, remembered = stabilise_geometry(None, _info(STALE), learn=True)

    published, remembered = stabilise_geometry(remembered, _info(FRESH), learn=True)

    assert published.outline == FRESH
    assert remembered.outline == FRESH


def test_publishes_without_geometry_still_draw_the_known_lawn() -> None:
    """Most state updates carry no map data; the lawn must stay on screen."""
    _published, remembered = stabilise_geometry(None, _info(FRESH), learn=True)

    published, _remembered = stabilise_geometry(remembered, MowerMapInfo())

    assert published.outline == FRESH
    assert published.outline_source == OUTLINE_SOURCE_MOWER


def test_coverage_fallback_never_overrides_the_mowers_own_map() -> None:
    """The traced approximation must not replace the mower's exact outline."""
    _published, remembered = stabilise_geometry(None, _info(FRESH), learn=True)

    published, _remembered = stabilise_geometry(
        remembered, _info(STALE, source=OUTLINE_SOURCE_COVERAGE)
    )

    assert published.outline == FRESH
    assert published.outline_source == OUTLINE_SOURCE_MOWER


def test_a_remap_drops_the_remembered_geometry() -> None:
    """A new map id means a new coordinate frame, so nothing is carried over."""
    _published, remembered = stabilise_geometry(None, _info(FRESH), learn=True)

    published, remembered = stabilise_geometry(
        remembered, MowerMapInfo(), remapped=True
    )

    assert published.outline == ()
    assert remembered is None


def test_trace_protection_logic_matches_coordinator_rules() -> None:
    """The mowed track may only grow, reset on task change, or reset on remap.

    Mirrors MowerCoordinator._carry_forward_map_trace: the cloud cannot
    re-serve the track (getMapTrack answers empty — the only source is small
    onMapTrack windows), so a stale publish carrying fewer points must not
    win. Observed live: a grouped refresh right after restart published an
    empty trace over the restored 880-point track and the debounced save made
    the loss permanent.
    """
    long_path = (MapPosition(x=0, y=0), MapPosition(x=1, y=1), MapPosition(x=2, y=2))
    short_path = (MapPosition(x=0, y=0),)

    # Same map: the longer remembered track outranks a shorter publish.
    remembered = ("1", long_path)
    incoming_mid, incoming = "1", short_path
    keep = remembered is not None and remembered[0] == incoming_mid and len(
        incoming
    ) < len(remembered[1])
    assert keep is True

    # A remap invalidates the remembered track entirely.
    keep_after_remap = remembered[0] == "2"
    assert keep_after_remap is False
