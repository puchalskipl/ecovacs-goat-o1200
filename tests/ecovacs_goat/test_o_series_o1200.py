"""Tests for O-series (O1200 LiDAR Pro) map, settings, and telemetry parsing.

Payload shapes in this module mirror a live-mowing debug capture of a GOAT
O1200 LiDAR Pro (firmware 2.13.10, 2026-08).
"""

import base64
from dataclasses import replace
import json
import lzma
from pathlib import Path
import struct
import sys
import types

import pytest

PACKAGE_PATH = Path(__file__).parents[2] / "custom_components" / "ecovacs_goat"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(PACKAGE_PATH.parent)]
sys.modules.setdefault("custom_components", custom_components)

ecovacs_goat = types.ModuleType("custom_components.ecovacs_goat")
ecovacs_goat.__path__ = [str(PACKAGE_PATH)]
sys.modules.setdefault("custom_components.ecovacs_goat", ecovacs_goat)

from custom_components.ecovacs_goat import mower_messages
from custom_components.ecovacs_goat.mower_messages import (
    apply_command_data,
    apply_mqtt_payload,
    apply_response,
)
from custom_components.ecovacs_goat.mower_models import (
    CUT_HEIGHT_MAX_MM,
    CUT_HEIGHT_MIN_MM,
    CUT_HEIGHT_STEP_MM,
    AreaParameter,
    MapPosition,
    MowerActivity,
    MowerLastJob,
    MowerState,
    cut_height_level_from_mm,
    cut_height_mm_from_level,
)

TOPIC = "iot/atr/{command}/did/cls/res/j"


def _compact_lzma_blob(payload: object) -> str:
    """Encode a payload the way ECOVACS wraps map blobs (base64 + LZMA1).

    The wrapper is: props byte, dict size (4 bytes LE), uncompressed size
    (4 bytes LE), raw LZMA1 stream — i.e. FORMAT_ALONE with the 8-byte size
    field shrunk to 4 bytes.
    """
    data = json.dumps(payload).encode()
    alone = lzma.compress(
        data, format=lzma.FORMAT_ALONE, filters=[{"id": lzma.FILTER_LZMA1}]
    )
    raw = alone[:5] + struct.pack("<I", len(data)) + alone[13:]
    return base64.b64encode(raw).decode()


def _mqtt(state: MowerState, command: str, data: dict) -> MowerState:
    """Apply an MQTT push with the captured envelope shape."""
    return apply_mqtt_payload(
        state,
        TOPIC.format(command=command),
        json.dumps({"header": {"tzm": 120, "ts": "1"}, "body": {"data": data}}),
    )


def _track_push(
    state: MowerState, coordinates: str, *, batid: str = "kpoiba"
) -> MowerState:
    """Apply one onMapTrack push carrying the given coordinate string."""
    return _mqtt(
        state,
        "onMapTrack",
        {
            "mid": "0",
            "totalWidth": 0,
            "totalHeight": 0,
            "resolution": 0,
            "batid": batid,
            "serial": "1",
            "index": "0",
            "info": _compact_lzma_blob([["1", "2", coordinates]]),
            "infoSize": 72,
        },
    )


def test_blob_helper_round_trips_through_shared_decoder() -> None:
    """The test encoder must produce blobs the integration's decoder reads."""
    payload = [["1", "2", "1;1;74;-20025,11849;-20025,13700"]]
    decoded = mower_messages._decode_map_subset(_compact_lzma_blob(payload))
    assert decoded == payload


def test_on_map_track_push_accumulates_track() -> None:
    """onMapTrack windows accumulate into the trace path across pushes."""
    state = MowerState()
    state = _track_push(state, "1;1;74;-20025,11849;-20025,13700")
    state = _track_push(state, "1;1;74;-20025,13700;-20025,14499", batid="second")

    points = [(p.x, p.y) for p in state.map.trace.path]
    assert points == [(-20025, 11849), (-20025, 13700), (-20025, 14499)]
    assert state.map.trace.batch_id == "second"
    # Header tokens ("1", "1", "74") must not be parsed as coordinates.
    assert all(abs(x) > 100 for x, _ in points)


def test_on_map_track_push_caps_accumulated_points(monkeypatch) -> None:
    """The accumulated track keeps only the newest points once at the cap."""
    monkeypatch.setattr(mower_messages, "O_SERIES_TRACK_MAX_POINTS", 3)
    state = MowerState()
    state = _track_push(state, "1;1;74;-1,1;-2,2;-3,3;-4,4")

    assert [(p.x, p.y) for p in state.map.trace.path] == [(-2, 2), (-3, 3), (-4, 4)]


def test_get_area_set_layer_still_uses_subsets_shape() -> None:
    """Map-set replies (subsets blob) keep feeding the areas layer."""
    state = MowerState()
    state = apply_command_data(
        state,
        "getAreaSet",
        {
            "mid": "1",
            "type": "ar",
            "subsets": _compact_lzma_blob(
                [["7", "0", "area", "", "-14600", "16950", "code"]]
            ),
        },
    )
    assert [(p.x, p.y) for p in state.map.areas] == [(-14600, 16950)]


def test_on_ari_decodes_obstacle_shapes() -> None:
    """onArI layer 3 carries the obstacle shapes the app paints on the lawn."""
    blob = _compact_lzma_blob(
        [
            ["1", "1", "0"],
            ["1", "3", "2", "100;-14600,16950;4328(2)6", "103;-19300,15550;38"],
            ["1", "4", "0"],
        ]
    )
    state = _mqtt(
        MowerState(),
        "onArI",
        {"mid": "1", "batid": "dknkcc", "serial": "1", "index": "0", "info": blob, "infoSize": 335},
    )

    obstacles = state.map.info.obstacles
    assert len(obstacles) == 2
    # Anchors are kept verbatim; the chain walks from there in CHAIN_STEP units.
    assert obstacles[0][0] == MapPosition(x=-14600, y=16950)
    assert obstacles[1][0] == MapPosition(x=-19300, y=15550)
    # Collinear runs are collapsed, so every shape stays a polygon but never
    # carries a point per chain step.
    assert all(len(shape) >= 3 for shape in obstacles)


def test_on_mi_decodes_lawn_outline() -> None:
    """onMI carries the mower's own lawn outline (the shape the app fills)."""
    blob = _compact_lzma_blob([["1", "s1;1;-24900,3750;3(3)1(3)7(3)5(3)"], ["2", "1"]])
    state = _mqtt(
        MowerState(),
        "onMI",
        {"mid": "1", "type": "-1", "info": blob, "infoSize": 120},
    )

    outline = state.map.info.outline
    # A 4x4 square walked in unit steps collapses to its corners.
    assert len(outline) == 5
    assert outline[0] == MapPosition(x=-24900, y=3750)
    assert state.map.info.outline_source == "mower"
    assert state.map.mid == "1"


def test_on_mi_placeholder_keeps_stored_outline() -> None:
    """A mid-job placeholder must not wipe the persisted outline."""
    stored = _mqtt(
        MowerState(),
        "onMI",
        {
            "mid": "1",
            "type": "-1",
            "info": _compact_lzma_blob([["1", "s1;1;-24900,3750;3(3)1(3)7(3)5(3)"]]),
        },
    )
    assert stored.map.info.outline

    # While a job runs the mower answers with an empty shape.
    after = _mqtt(
        stored,
        "onMI",
        {"mid": "1", "type": "-1", "info": _compact_lzma_blob([["1", "s1;0;"], ["2", "0"]])},
    )
    assert after.map.info.outline == stored.map.info.outline


def test_on_area_parameter_updates_settings() -> None:
    """onAreaParameter pushes update the typed per-zone parameters."""
    state = _mqtt(
        MowerState(),
        "onAreaParameter",
        {
            "areaParameters": [
                {
                    "areaID": "1",
                    "mowHeightLevel": 6,
                    "cutMode": 7,
                    "obstacleHeight": 2,
                    "angle": 270,
                }
            ]
        },
    )
    assert state.settings.area_parameters == (
        AreaParameter(
            area_id=1, mow_height_level=6, cut_mode=7, obstacle_height=2, angle=270
        ),
    )


def test_get_area_parameter_via_grouped_get_info() -> None:
    """getAreaParameter inside a grouped getInfo reply is applied."""
    state = apply_response(
        MowerState(),
        "getInfo",
        {
            "body": {
                "data": {
                    "getAreaParameter": {
                        "data": {
                            "areaParameters": [{"areaID": 1, "mowHeightLevel": 7}]
                        }
                    }
                }
            }
        },
    )
    assert state.settings.area_parameters[0].mow_height_level == 7


def test_area_parameter_payload_round_trip() -> None:
    """AreaParameter serialises back to the ECOVACS record shape."""
    parameter = AreaParameter(
        area_id=1, mow_height_level=6, cut_mode=7, obstacle_height=2, angle=270
    )
    assert parameter.as_payload() == {
        "areaID": "1",
        "mowHeightLevel": 6,
        "cutMode": 7,
        "obstacleHeight": 2,
        "angle": 270,
    }


def test_bd_telemetry_topics_update_telemetry() -> None:
    """onFwBuryPoint-bd_* pushes (no data envelope) fill telemetry fields."""
    state = MowerState()
    for command, body in (
        ("onFwBuryPoint-bd_batterytemp", {"temperature": 35, "gid": "G1"}),
        (
            "onFwBuryPoint-bd_batteryinfo",
            {"batteryCurrent": 2636, "batteryLevel": 71, "batteryVoltage": 18338},
        ),
        (
            "onFwBuryPoint-bd_power",
            {
                "corePlateVoltage": 12069,
                "motorDriveVoltage": 12057,
                "motorVoltage": 18349,
                "systemVoltage": 18420,
            },
        ),
    ):
        state = apply_mqtt_payload(
            state,
            TOPIC.format(command=command),
            json.dumps({"header": {"ts": "1"}, "body": body}),
        )

    assert state.telemetry.battery_temperature == 35
    assert state.telemetry.battery_level == 71
    assert state.telemetry.battery_current == 2636
    assert state.telemetry.battery_voltage == 18338
    assert state.telemetry.system_voltage == 18420
    assert state.telemetry.motor_voltage == 18349
    assert state.telemetry.motor_drive_voltage == 12057
    assert state.telemetry.core_plate_voltage == 12069


def test_on_stats_progress_from_area_ratio() -> None:
    """onStats without an explicit progress field derives it from areas."""
    state = _mqtt(
        MowerState(),
        "onStats",
        {"time": 11297, "area": 2526725, "mowedArea": 1746900},
    )
    # Progress is rounded to whole percent to keep the history clean.
    assert state.stats.progress == pytest.approx(69)
    assert state.stats.job_area == 2526725
    assert state.stats.area == 1746900


@pytest.mark.parametrize(
    ("level", "millimetres"),
    [(1, 80), (6, 55), (7, 50), (11, 30)],
)
def test_cutting_height_level_maps_to_millimetres(level: int, millimetres: int) -> None:
    """Cutting height is inverse to mowHeightLevel: mm = 85 - 5 * level.

    All four pairs are app-calibrated readings from an O1200 LiDAR Pro.
    """
    assert cut_height_mm_from_level(level) == millimetres
    assert cut_height_level_from_mm(millimetres) == level


def test_cutting_height_bounds_match_calibrated_levels() -> None:
    """The exposed slider range covers exactly levels 11..1."""
    assert cut_height_level_from_mm(CUT_HEIGHT_MIN_MM) == 11
    assert cut_height_level_from_mm(CUT_HEIGHT_MAX_MM) == 1
    assert (CUT_HEIGHT_MAX_MM - CUT_HEIGHT_MIN_MM) % CUT_HEIGHT_STEP_MM == 0


def test_chunked_on_info_reply_is_reassembled() -> None:
    """Large grouped getInfo replies arrive split across onInfo fragments.

    The mower splits any reply over the MQTT payload limit into
    ``{d_id, d_seq, d_sum, d_val}`` text fragments; only the concatenation is
    valid JSON, so without reassembly every setting in that group is lost.
    """
    reply = json.dumps(
        {
            "header": {"tzm": 120},
            "body": {
                "code": 0,
                "msg": "ok",
                "data": {
                    "getAnimProtect": {
                        "data": {"enable": 1, "start": "21:0", "end": "8:0"},
                        "code": 0,
                    },
                    "getRainDelay": {"data": {"enable": 1, "delay": 180}, "code": 0},
                },
            },
        }
    )
    half = len(reply) // 2
    store: dict[str, dict[int, str]] = {}

    assert (
        mower_messages.merge_info_chunks(
            store, {"d_id": "903583", "d_seq": "0", "d_sum": "2", "d_val": reply[:half]}
        )
        is None
    )
    assert store, "the first fragment must be buffered"

    merged = mower_messages.merge_info_chunks(
        store, {"d_id": "903583", "d_seq": "1", "d_sum": "2", "d_val": reply[half:]}
    )
    assert merged is not None
    assert not store, "a completed batch must be dropped from the buffer"

    state = apply_command_data(
        MowerState(), "getInfo", mower_messages.body_data(merged)
    )
    assert state.settings.animal_enabled is True
    assert state.settings.animal_start == "21:00"
    assert state.settings.rain_delay == 180


def test_out_of_order_info_fragments_still_merge() -> None:
    """Fragments may arrive out of order; d_seq defines the assembly order."""
    reply = json.dumps({"body": {"data": {"getRainDelay": {"data": {"delay": 90}}}}})
    half = len(reply) // 2
    store: dict[str, dict[int, str]] = {}

    assert (
        mower_messages.merge_info_chunks(
            store, {"d_id": "1", "d_seq": "1", "d_sum": "2", "d_val": reply[half:]}
        )
        is None
    )
    merged = mower_messages.merge_info_chunks(
        store, {"d_id": "1", "d_seq": "0", "d_sum": "2", "d_val": reply[:half]}
    )
    assert merged is not None
    state = apply_command_data(
        MowerState(), "getInfo", mower_messages.body_data(merged)
    )
    assert state.settings.rain_delay == 90


def test_protect_state_does_not_overwrite_settings() -> None:
    """Regression: runtime protection flags must not clobber the settings.

    ``getProtectState`` reports whether a protection is active *right now*;
    animal protection with a 21:00-08:00 window reports 0 at midday. Letting
    it write ``animal_enabled`` flipped the switch back off seconds after the
    app turned it on.
    """
    state = _mqtt(
        MowerState(),
        "onAnimProtect",
        {"enable": 1, "start": "21:00", "end": "08:00"},
    )
    assert state.settings.animal_enabled is True

    state = _mqtt(
        state,
        "onProtectState",
        {
            "isAnimProtect": 0,
            "isRainProtect": 0,
            "isRainDelay": 0,
            "isEStop": 0,
            "isLocked": 0,
        },
    )
    assert state.settings.animal_enabled is True
    assert state.settings.animal_start == "21:00"
    assert state.protections.animal_active is False
    assert state.protections.emergency_stop is False


def test_protect_state_reports_runtime_lock_flag() -> None:
    """``isLocked`` is a runtime flag surfaced via the protections summary."""
    state = _mqtt(MowerState(), "onProtectState", {"isLocked": 0})
    assert state.protections.locked is False


def test_auto_cut_direction_toggle() -> None:
    """onAutoCutDirection carries the weekly direction-change setting."""
    state = _mqtt(MowerState(), "onAutoCutDirection", {"enable": 0})
    assert state.settings.auto_cut_direction is False
    state = _mqtt(state, "onAutoCutDirection", {"enable": 1})
    assert state.settings.auto_cut_direction is True


def test_volume_push_keeps_untouched_levels() -> None:
    """onVolume fills every level; a partial push keeps the cached ones."""
    state = _mqtt(
        MowerState(),
        "onVolume",
        {"total": 10, "volume": 6, "fallVolume": 10, "searchVolume": 10},
    )
    assert state.settings.volume == 6
    assert state.settings.fall_volume == 10
    assert state.settings.search_volume == 10
    assert state.settings.volume_total == 10

    state = _mqtt(state, "onVolume", {"fallVolume": 2})
    assert state.settings.fall_volume == 2
    assert state.settings.volume == 6
    assert state.settings.volume_total == 10


def test_area_parameter_push_syncs_obstacle_avoidance() -> None:
    """Per-zone obstacleHeight drives the shared obstacle-avoidance setting."""
    state = _mqtt(
        MowerState(),
        "onAreaParameter",
        {"areaParameters": [{"areaID": "1", "obstacleHeight": 3}]},
    )
    assert state.settings.obstacle_avoidance == "bumpy_tall_grass"
    assert state.settings.area_parameters[0].obstacle_height == 3


def test_clean_info_reports_edge_trim_work_mode() -> None:
    """The edge-trim job (borderrotate) is exposed as the active clean type."""
    state = _mqtt(
        MowerState(),
        "onCleanInfo",
        {
            "trigger": "app",
            "state": "clean",
            "cleanState": {
                "motionState": "working",
                "cid": "122",
                "content": {
                    "type": "borderrotate",
                    "value": "reid:1;",
                    "subContent": {"type": "borderrotate"},
                },
            },
        },
    )
    assert state.activity is MowerActivity.MOWING
    assert state.clean_type == "borderrotate"

    state = _mqtt(
        state, "onCleanInfo", {"trigger": "none", "state": "idle"}
    )
    assert state.clean_type is None


def test_docked_zero_position_is_the_charging_station() -> None:
    """A docked mower reports (0, 0) — the map origin IS the station.

    The marker moves to the dock, the station location is learned from it
    (``chargePos`` itself always comes back invalid), and the dock point stays
    out of the mowing trail so the path does not draw a line to the origin.
    """
    state = replace(MowerState(), activity=MowerActivity.MOWING)
    state = _mqtt(
        state,
        "onPos",
        {"deebotPos": {"x": -940, "y": -441, "a": -34, "invalid": 0}, "mid": "0"},
    )
    assert state.map.position_history

    state = _mqtt(
        state,
        "onPos",
        {"deebotPos": {"x": 0, "y": 0, "a": 0, "invalid": 0}, "mid": "0"},
    )
    assert state.map.current_position.x == 0
    assert state.map.current_position.y == 0
    assert state.map.charge_positions == (MapPosition(x=0, y=0),)
    # The dock point must not extend the mowing trail.
    assert state.map.position_history[-1] == MapPosition(
        x=-940, y=-441, a=-34, invalid=0
    )


def test_position_placeholder_mid_does_not_wipe_geometry() -> None:
    """Regression: onPos mid "0" after onMI mid "1" must keep the track.

    O-series position pushes always report mid "0"; the real map id arrives on
    map replies. Treating "0" as a map switch wiped the live geometry seconds
    after every map reply.
    """
    state = MowerState()
    state = _track_push(state, "1;1;74;-20025,11849;-20025,13700")
    state = _mqtt(state, "onMI", {"mid": "1", "centerX": -12450, "centerY": 8300})
    assert state.map.mid == "1"

    state = _mqtt(
        state,
        "onPos",
        {"deebotPos": {"x": -20011, "y": 14243, "a": -109, "invalid": 0}, "mid": "0"},
    )
    assert state.map.mid == "1"
    assert len(state.map.trace.path) == 2

    # A real remap (a different non-placeholder id) still resets geometry.
    state = _mqtt(
        state,
        "onPos",
        {"deebotPos": {"x": 0, "y": 0, "a": 0, "invalid": 0}, "mid": "2"},
    )
    assert state.map.mid == "2"
    assert state.map.trace.path == ()


def test_last_job_record_roundtrip() -> None:
    """Last-job records survive the persistence round-trip unchanged."""
    record = MowerLastJob(
        kind="borderrotate",
        started_at="2026-08-27T08:26:50+00:00",
        ended_at="2026-08-27T08:39:48+00:00",
        mowed_area=11.1,
        duration_minutes=12.9,
        task_id="1072906304",
    )
    assert MowerLastJob.from_payload(record.as_dict()) == record


def test_last_job_record_rejects_invalid_payloads() -> None:
    """Broken persisted payloads are dropped instead of crashing the load."""
    assert MowerLastJob.from_payload(None) is None
    assert MowerLastJob.from_payload({}) is None
    assert MowerLastJob.from_payload({"kind": ""}) is None
    partial = MowerLastJob.from_payload({"kind": "auto", "mowed_area": "bad"})
    assert partial is not None
    assert partial.mowed_area is None
    assert partial.ended_at is None


def test_last_jobs_survive_state_updates() -> None:
    """State updates built via replace() keep the tracked last_jobs mapping."""
    record = MowerLastJob(kind="auto", ended_at="2026-08-27T08:00:00+00:00")
    state = replace(MowerState(), last_jobs={"auto": record})
    state = apply_command_data(
        state, "getTotalStats", {"area": 4963, "time": 177060, "count": 45}
    )
    assert state.last_jobs == {"auto": record}


def test_partial_protect_state_does_not_clear_active_protection() -> None:
    """A partial getProtectState push must not drop an active protection.

    Observed live: during the animal-protection window the sensor flapped
    animal -> none -> animal every few seconds, because pushes that omit
    ``isAnimProtect`` rebuilt every flag from scratch. That made the dashboard
    unblock the controls while the mower was in fact refusing to start.
    """
    state = _mqtt(MowerState(), "onProtectState", {"isAnimProtect": 1, "isRainProtect": 0})
    assert state.protections.animal_active is True

    # A later push carries only the rain flags.
    state = _mqtt(state, "onProtectState", {"isRainProtect": 0, "isRainDelay": 0})
    assert state.protections.animal_active is True
    assert state.protections.rain_active is False

    # An explicit zero does clear it.
    state = _mqtt(state, "onProtectState", {"isAnimProtect": 0})
    assert state.protections.animal_active is False
