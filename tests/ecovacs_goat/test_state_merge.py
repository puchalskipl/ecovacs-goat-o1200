"""The three-way merge that stops stale refreshes reverting pushed state."""

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

from dataclasses import replace

from custom_components.ecovacs_goat.mower_messages import (
    apply_command_data,
    apply_mqtt_payload,
)
from custom_components.ecovacs_goat.mower_models import (
    MapPosition,
    MowerActivity,
    MowerState,
    MowerStats,
    NetworkInfo,
)
from custom_components.ecovacs_goat.state_merge import (
    changed_field_names,
    merge_refreshed_state,
)


def test_refresh_cannot_revert_activity_a_push_updated_during_its_awaits() -> None:
    """The phantom-job killer.

    Observed live: a job ends (push sets IDLE), then an in-flight grouped
    refresh — snapshotted while MOWING — publishes MOWING again, reopening a
    phantom job in the coordinator's lifecycle tracking and bouncing the
    status tile for a minute.
    """
    base = MowerState(activity=MowerActivity.MOWING)
    current = replace(base, activity=MowerActivity.IDLE)
    refreshed = replace(base, battery=90)

    merged = merge_refreshed_state(base, refreshed, current)

    assert merged.activity is MowerActivity.IDLE
    assert merged.battery == 90


def test_refresh_still_contributes_fields_only_it_polls() -> None:
    """Pushes never carry lifespans/network; the refresh must land them."""
    base = MowerState(battery=50)
    current = replace(base, battery=49)  # a push moved the battery meanwhile
    refreshed = replace(
        base,
        lifespans={"blade": 35.0},
        network=NetworkInfo(ssid="ogrod", rssi=-61),
        firmware_version="2.13.10",
    )

    merged = merge_refreshed_state(base, refreshed, current)

    assert merged.battery == 49
    assert merged.lifespans == {"blade": 35.0}
    assert merged.network.ssid == "ogrod"
    assert merged.firmware_version == "2.13.10"


def test_untouched_fields_take_the_refreshed_value() -> None:
    """With nothing pushed in between, the refresh goes through unchanged."""
    base = MowerState(battery=50, activity=MowerActivity.DOCKED)
    refreshed = replace(base, battery=51, charging=True)

    merged = merge_refreshed_state(base, refreshed, base)

    assert merged == refreshed


def test_first_publish_without_prior_state_passes_refreshed_through() -> None:
    refreshed = MowerState(battery=77)
    assert merge_refreshed_state(None, refreshed, None) is refreshed
    assert merge_refreshed_state(None, refreshed, refreshed) is refreshed


def test_nested_stats_merge_field_by_field() -> None:
    """A push moving progress must not starve the refresh's totals.

    Totals come only from getTotalStats; progress moves on every onStats
    push. A whole-object rule would drop the totals for the entire duration
    of every job.
    """
    base = MowerState(stats=MowerStats(progress=10.0, total_area=1000))
    current = replace(base, stats=replace(base.stats, progress=12.0))
    refreshed = replace(base, stats=replace(base.stats, total_area=1010))

    merged = merge_refreshed_state(base, refreshed, current)

    assert merged.stats.progress == 12.0
    assert merged.stats.total_area == 1010


def test_pushed_map_position_survives_a_stale_map_refresh() -> None:
    """The marker used to jump backwards on every slow map refresh."""
    base = MowerState()
    base = replace(
        base, map=replace(base.map, current_position=MapPosition(x=0, y=0))
    )
    current = replace(
        base, map=replace(base.map, current_position=MapPosition(x=500, y=0))
    )
    refreshed = replace(base, map=replace(base.map, mid="1"))

    merged = merge_refreshed_state(base, refreshed, current)

    assert merged.map.current_position == MapPosition(x=500, y=0)
    assert merged.map.mid == "1"


def test_available_true_after_a_successful_refresh() -> None:
    """A completed refresh is itself proof of reachability."""
    base = MowerState(available=False)
    current = replace(base, available=False)
    refreshed = replace(base, available=True)

    merged = merge_refreshed_state(base, refreshed, current)

    assert merged.available is True


def test_raw_union_prefers_push_updated_keys() -> None:
    base = MowerState(raw={"onStats": {"progress": 10}})
    current = replace(base, raw={"onStats": {"progress": 12}})
    refreshed = replace(
        base, raw={**base.raw, "getWifiList": {"ssid": "ogrod"}}
    )

    merged = merge_refreshed_state(base, refreshed, current)

    assert merged.raw["onStats"] == {"progress": 12}
    assert merged.raw["getWifiList"] == {"ssid": "ogrod"}


def test_end_of_job_push_is_not_reopened_by_an_inflight_refresh() -> None:
    """Through the real message layer, not hand-built states.

    base: the job is running (getCleanInfo reports a working auto clean).
    current: an onCleanInfo push ended it (idle) after the refresh began.
    refreshed: the in-flight refresh applies a stale HTTP body still saying
    "working" onto its base. The merged state — the very object the
    coordinator's job lifecycle sees — must not report the job open again.
    """
    working = {
        "getCleanInfo": {
            "state": "clean",
            "cleanState": {
                "motionState": "working",
                "content": {"type": "auto"},
            },
            "trigger": "app",
        }
    }
    idle_push = (
        '{"body": {"data": {"state": "idle", "trigger": "workComplete"}}}'
    )

    base = apply_command_data(MowerState(), "getInfo", working)
    assert base.activity is MowerActivity.MOWING

    current = apply_mqtt_payload(
        base, "iot/atr/onCleanInfo/x/y/z/j", idle_push
    )
    assert current.activity is not MowerActivity.MOWING

    refreshed = apply_command_data(base, "getInfo", working)

    merged = merge_refreshed_state(base, refreshed, current)

    assert merged.activity is not MowerActivity.MOWING
    assert merged.clean_type is None


def test_changed_field_names_reports_top_level_diffs() -> None:
    a = MowerState(battery=10, charging=True)
    b = replace(a, battery=11, error_code=3)
    assert changed_field_names(a, b) == ("battery", "error_code")
    assert changed_field_names(a, a) == ()


def test_session_progress_never_regresses_within_one_task() -> None:
    """The observed sawtooth: an HTTP getStats body older than a push that
    landed before the refresher's snapshot (base == current, refreshed wins
    with the older number). Within one task the larger value is truer."""
    base = MowerState(task_id="t1", stats=MowerStats(progress=7.0, area=120))
    refreshed = replace(base, stats=replace(base.stats, progress=6.0, area=110))

    merged = merge_refreshed_state(base, refreshed, base)

    assert merged.stats.progress == 7.0
    assert merged.stats.area == 120


def test_progress_clamp_releases_on_a_new_task_id() -> None:
    base = MowerState(task_id="t1", stats=MowerStats(progress=90.0))
    refreshed = replace(
        base, task_id="t2", stats=replace(base.stats, progress=0.0)
    )

    merged = merge_refreshed_state(base, refreshed, base)

    assert merged.stats.progress == 0.0


def test_clamp_skips_none_values() -> None:
    base = MowerState(task_id="t1", stats=MowerStats(progress=None, area=50))
    refreshed = replace(base, stats=replace(base.stats, area=None))

    merged = merge_refreshed_state(base, refreshed, base)

    assert merged.stats.progress is None
    # area: refreshed says None, current says 50 -> clamp skips (None side),
    # the plain merge rule already decided; nothing throws.
