"""Tests for ECOVACS mower message parsing."""

from pathlib import Path
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

from custom_components.ecovacs_goat.mower_messages import (
    apply_command_data,
    apply_mqtt_payload,
    apply_response,
)
from custom_components.ecovacs_goat.mower_models import (
    MapPosition,
    MowerActivity,
    MowerMap,
    MowerMapInfo,
    MowerMapTrace,
    MowerState,
)
from custom_components.ecovacs_goat.mower_api import (
    EcovacsApiError,
    _raise_for_control_error,
)


def test_grouped_get_info_updates_core_state() -> None:
    """Grouped app getInfo responses update core state in one pass."""
    state = apply_response(
        MowerState(),
        "getInfo",
        {
            "body": {
                "data": {
                    "getBattery": {"data": {"value": 95, "isLow": 0}},
                    "getCleanInfo_V2": {"data": {"trigger": "none", "state": "idle"}},
                    "getChargeState": {
                        "data": {"isCharging": 1, "mode": "slot"},
                    },
                    "getError": {"data": {"code": [0]}},
                }
            }
        },
    )

    assert state.battery == 95
    assert state.activity is MowerActivity.DOCKED
    assert state.charging is True
    assert state.charge_mode == "slot"
    assert state.error_code == 0


def test_grouped_get_info_keeps_docked_when_idle_follows_charging() -> None:
    """Captured getInfo ordering reports charge state before idle clean state."""
    state = apply_response(
        MowerState(),
        "getInfo",
        {
            "body": {
                "data": {
                    "getChargeState": {
                        "data": {"isCharging": 1, "mode": "slot"},
                    },
                    "getCleanInfo_V2": {"data": {"trigger": "none", "state": "idle"}},
                }
            }
        },
    )

    assert state.activity is MowerActivity.DOCKED
    assert state.charging is True


def test_grouped_get_info_reports_paused_when_clean_task_is_charging() -> None:
    """A charging pause during a clean task should not report as mowing."""
    state = apply_response(
        MowerState(),
        "getInfo",
        {
            "body": {
                "data": {
                    "getChargeState": {
                        "data": {"isCharging": 1, "mode": "slot"},
                    },
                    "getCleanInfo_V2": {
                        "data": {
                            "trigger": "none",
                            "state": "clean",
                            "cleanState": {"motionState": "pause"},
                        }
                    },
                }
            }
        },
    )

    assert state.activity is MowerActivity.PAUSED
    assert state.charging is True


def test_grouped_get_info_caches_task_id_for_app_style_writes() -> None:
    """Captured write payloads reuse the current task id from stats readbacks."""
    state = apply_response(
        MowerState(),
        "getInfo",
        {
            "body": {
                "data": {
                    "getStats": {
                        "data": {
                            "mowid": "12345",
                            "time": 1,
                            "area": 2538175,
                            "mowedArea": 1269088,
                        }
                    },
                    "getLastTimeStats": {"data": {"cid": "12345", "stop": 1}},
                }
            }
        },
    )

    assert state.task_id == "12345"
    assert state.stats.area == 1269088
    assert state.stats.job_area == 2538175
    assert state.stats.progress == 50.0


def test_stats_prefers_reported_progress_when_available() -> None:
    """The app may report progress separately from mowed-area ratio."""
    state = apply_command_data(
        MowerState(),
        "getStats",
        {
            "area": 2538175,
            "mowedArea": 1269088,
            "progress": 93,
        },
    )

    assert state.stats.area == 1269088
    assert state.stats.job_area == 2538175
    assert state.stats.progress == 93


def test_mqtt_setting_push_updates_cache() -> None:
    """Mower-specific MQTT pushes update settings without polling."""
    state = apply_mqtt_payload(
        MowerState(),
        "iot/atr/onAnimProtect/endpoint/77atlz/ONb7/j",
        b'{"body":{"data":{"enable":1,"start":"20:0","end":"8:0"}}}',
    )

    assert state.settings.animal_enabled is True
    assert state.settings.animal_start == "20:00"
    assert state.settings.animal_end == "08:00"


def test_mqtt_cut_efficiency_push_updates_cache() -> None:
    """Captured mowing efficiency pushes update settings without polling."""
    state = apply_mqtt_payload(
        MowerState(),
        "iot/atr/onCutEfficiency/endpoint/77atlz/ONb7/j",
        b'{"header":{"ver":"0.0.1"},"body":{"data":{"level":2},"code":0,"msg":"ok"}}',
    )

    assert state.settings.mowing_efficiency == "delicate"


def test_captured_mqtt_setting_burst_updates_cache() -> None:
    """Captured settings changed in the app update the cache from pushes."""
    state = MowerState()
    for topic, payload in (
        (
            "iot/atr/onCutDirection/endpoint/77atlz/ONb7/j",
            b'{"body":{"data":{"angle":90,"set":1}}}',
        ),
        (
            "iot/atr/onRainDelay/endpoint/77atlz/ONb7/j",
            b'{"body":{"data":{"enable":0,"delay":180}}}',
        ),
        (
            "iot/atr/onAnimProtect/endpoint/77atlz/ONb7/j",
            b'{"body":{"data":{"enable":0,"start":"21:00","end":"08:00"}}}',
        ),
        (
            "iot/atr/onBorderSwitch/endpoint/77atlz/ONb7/j",
            b'{"body":{"data":{"enable":0,"mode":0}}}',
        ),
        (
            "iot/atr/onObstacleHeight/endpoint/77atlz/ONb7/j",
            b'{"body":{"data":{"level":3}}}',
        ),
        (
            "iot/atr/onRecognization/endpoint/77atlz/ONb7/j",
            b'{"body":{"data":{"state":1,"update":0,"items":[]}}}',
        ),
    ):
        state = apply_mqtt_payload(state, topic, payload)

    assert state.settings.cut_direction == 90
    assert state.settings.rain_enabled is False
    assert state.settings.rain_delay == 180
    assert state.settings.animal_enabled is False
    assert state.settings.animal_start == "21:00"
    assert state.settings.animal_end == "08:00"
    assert state.settings.border_switch is False
    assert state.settings.border_mode == 0
    assert state.settings.obstacle_avoidance == "bumpy_tall_grass"
    assert state.settings.ai_recognition is True


def test_captured_lifecycle_and_battery_pushes_update_cache() -> None:
    """Captured mower command pushes update activity and battery without polling."""
    state = MowerState()
    for topic, payload in (
        (
            "iot/atr/onCleanInfo_V2/endpoint/77atlz/ONb7/j",
            b'{"body":{"data":{"trigger":"none","state":"clean","cleanState":{"motionState":"working"}}}}',
        ),
        (
            "iot/atr/onBattery/endpoint/77atlz/ONb7/j",
            b'{"body":{"data":{"value":94,"isLow":0}}}',
        ),
        (
            "iot/atr/onCleanInfo_V2/endpoint/77atlz/ONb7/j",
            b'{"body":{"data":{"trigger":"none","state":"clean","cleanState":{"motionState":"pause"}}}}',
        ),
        (
            "iot/atr/onCleanInfo_V2/endpoint/77atlz/ONb7/j",
            b'{"body":{"data":{"trigger":"none","state":"goCharging","cleanState":{"motionState":"goCharging"}}}}',
        ),
    ):
        state = apply_mqtt_payload(state, topic, payload)

    assert state.battery == 94
    assert state.activity is MowerActivity.RETURNING


def test_clean_pause_push_reports_paused_even_when_state_is_clean() -> None:
    """The app can report state=clean while motionState carries the pause."""
    state = apply_mqtt_payload(
        MowerState(activity=MowerActivity.MOWING),
        "iot/atr/onCleanInfo_V2/endpoint/77atlz/ONb7/j",
        b'{"body":{"data":{"trigger":"none","state":"clean","cleanState":{"motionState":"pause"}}}}',
    )

    assert state.activity is MowerActivity.PAUSED


def test_clean_working_clears_stale_charging_state() -> None:
    """A resumed working payload should win over stale charging state."""
    state = apply_mqtt_payload(
        MowerState(charging=True, activity=MowerActivity.DOCKED),
        "iot/atr/onCleanInfo_V2/endpoint/77atlz/ONb7/j",
        b'{"body":{"data":{"trigger":"none","state":"clean","cleanState":{"motionState":"working"}}}}',
    )

    assert state.activity is MowerActivity.MOWING
    assert state.charging is False


def test_protect_state_does_not_overwrite_rain_delay_setting() -> None:
    """Protection-state pushes are not the same as the rain-sensor setting."""
    state = apply_command_data(
        MowerState(),
        "getRainDelay",
        {"enable": 1, "delay": 180},
    )
    state = apply_command_data(
        state,
        "onProtectState",
        {"isAnimProtect": 0, "isRainProtect": 1, "isRainDelay": 0, "isLocked": 0},
    )

    assert state.settings.rain_enabled is True
    assert state.settings.rain_delay == 180


def test_stats_network_and_lifespan_parsing() -> None:
    """Direct app readbacks update diagnostics."""
    state = apply_command_data(
        MowerState(),
        "getWifiList",
        {
            "mac": "02:00:00:00:00:01",
            "list": [{"ssid": "Example WiFi", "rssi": 64, "ip": "192.0.2.10"}],
        },
    )
    state = apply_command_data(
        state,
        "getLifeSpan",
        [
            {"type": "blade", "left": 3367, "total": 4800},
            {"type": "lensBrush", "left": 1000, "total": 1000},
        ],
    )
    state = apply_command_data(
        state,
        "getTotalStats",
        {"area": 26067, "time": 647760, "count": 131},
    )

    assert state.network.ip == "192.0.2.10"
    assert state.network.rssi == 64
    # Percentages are stored as whole percent (see _progress/lifespan parsing).
    assert state.lifespans["blade"] == 70
    assert state.lifespans["lensBrush"] == 100
    assert state.stats.total_count == 131
    # Lifetime totals are reported in m2 (unlike onStats, which uses cm2)
    # and must stay unconverted.
    assert state.stats.total_area == 26067
    assert state.stats.total_duration == 647760


def test_get_robot_feature_populates_state() -> None:
    """getRobotFeature from grouped getInfo is merged into robot_features."""
    state = apply_response(
        MowerState(),
        "getInfo",
        {
            "body": {
                "data": {
                    "getRobotFeature": {
                        "data": {"4g": 1, "gps": 0, "station": 0},
                        "code": 0,
                        "msg": "ok",
                    },
                }
            }
        },
    )
    assert state.robot_features == {"4g": 1, "gps": 0, "station": 0}


def test_ngiot_body_code_failure_raises_api_error() -> None:
    """N-GIoT responses report command failures in body.code."""
    with pytest.raises(EcovacsApiError):
        _raise_for_control_error(
            "clean_V2",
            {"body": {"code": 500, "msg": "Request Timeout"}},
        )


def test_ngiot_null_json_body_is_not_an_error() -> None:
    """Some models return JSON null on successful control (no structured payload)."""
    _raise_for_control_error("clean_V2", None)
    _raise_for_control_error("appping", None)


def test_scheduled_clean_info_reports_mowing() -> None:
    """A scheduled job that fires on the mower should report as mowing (issue #7)."""
    state = apply_mqtt_payload(
        MowerState(activity=MowerActivity.IDLE),
        "iot/atr/onCleanInfo_V2/endpoint/77atlz/ONb7/j",
        b'{"body":{"data":{"trigger":"schedule","state":"working"}}}',
    )

    assert state.activity is MowerActivity.MOWING


def test_scheduled_trigger_does_not_override_returning() -> None:
    """A scheduled trigger reporting goCharging must not be forced to mowing."""
    state = apply_command_data(
        MowerState(),
        "getCleanInfo_V2",
        {"trigger": "schedule", "state": "goCharging"},
    )

    assert state.activity is MowerActivity.RETURNING


def test_o_series_clean_info_uses_shared_parser() -> None:
    """The O800 RTK getCleanInfo payload has the same fields as G1 getCleanInfo_V2."""
    state = apply_command_data(
        MowerState(),
        "getCleanInfo",
        {
            "trigger": "app",
            "other": "0",
            "state": "clean",
            "cleanState": {
                "motionState": "working",
                "cid": "122",
                "content": {"type": "auto", "subContent": {"type": "auto"}},
            },
        },
    )

    assert state.activity is MowerActivity.MOWING
    assert state.task_id == "122"


def test_o_series_rtk_position_drives_live_marker() -> None:
    """O-series getPos reports rtkPos instead of uwbPos; the marker still works."""
    state = apply_command_data(
        MowerState(activity=MowerActivity.MOWING),
        "getPos",
        {
            "deebotPos": {"x": 120, "y": -45, "a": 30, "invalid": 0},
            "chargePos": [{"x": 0, "y": 0, "a": 0, "t": 1, "invalid": 0}],
            "rtkPos": [{"x": 10, "y": 20, "invalid": 0}],
            "mid": "1",
        },
    )

    assert state.map.current_position is not None
    assert state.map.current_position.x == 120
    assert state.map.uwb_positions and state.map.uwb_positions[0].x == 10
    assert state.map.mid == "1"


def _state_with_decoded_map(mid: str) -> MowerState:
    """Build a state that already has decoded base map geometry for ``mid``."""
    outline = (MapPosition(x=0, y=0), MapPosition(x=10, y=0), MapPosition(x=10, y=10))
    return MowerState(
        map=MowerMap(
            mid=mid,
            current_position=MapPosition(x=5, y=5),
            charge_positions=(MapPosition(x=0, y=0),),
            uwb_positions=(MapPosition(x=1, y=1),),
            position_history=(MapPosition(x=2, y=2), MapPosition(x=3, y=3)),
            info=MowerMapInfo(batch_id="old", outline=outline),
            trace=MowerMapTrace(
                batch_id="old",
                lanes={"1": ((MapPosition(x=4, y=4), MapPosition(x=4, y=9)),)},
            ),
            revision=7,
        )
    )


def test_remap_new_map_id_clears_stale_geometry() -> None:
    """A new map id (mower reset + remap) drops geometry from the old map frame."""
    state = _state_with_decoded_map("100")

    state = apply_command_data(
        state,
        "getPos",
        {
            "deebotPos": {"x": 50, "y": 60, "a": 90, "invalid": 0},
            "mid": "200",
        },
    )

    assert state.map.mid == "200"
    assert state.map.info.outline == ()
    assert state.map.info.batch_id is None
    assert state.map.trace.lanes == {}
    assert state.map.position_history == ()
    assert state.map.charge_positions == ()
    assert state.map.uwb_positions == ()
    assert state.map.revision > 7
    assert state.map.current_position is not None
    assert state.map.current_position.x == 50


def test_same_map_id_keeps_existing_geometry() -> None:
    """Repeated payloads for the same map id never discard decoded geometry."""
    state = _state_with_decoded_map("100")

    state = apply_command_data(
        state,
        "getPos",
        {
            "deebotPos": {"x": 50, "y": 60, "a": 90, "invalid": 0},
            "mid": "100",
        },
    )

    assert state.map.mid == "100"
    assert state.map.info.outline != ()
    assert state.map.trace.lanes != {}


def test_base_map_reply_does_not_re_own_active_map_id() -> None:
    """Base-map replies feed geometry but never re-own the active map id.

    The G1 position stream and the ``getMapInfo_V2`` geometry reply can report
    ``mid`` values from different namespaces, so a base-map reply must be
    applied for the active map (not discarded) while leaving the active map id
    owned by the live position stream. Discarding it was the regression that
    made the mowed-area outline disappear.
    """
    state = _state_with_decoded_map("100")

    state = apply_command_data(
        state,
        "onMapInfo_V2",
        {"mid": "200", "batid": "fresh", "serial": "0", "type": "0", "index": 0,
         "info": "ignored"},
    )

    assert state.map.mid == "100"
    assert state.map.info.batch_id == "fresh"


def test_trace_reply_does_not_re_own_active_map_id() -> None:
    """Trace replies advance the trace but never re-own the active map id.

    Letting a trace reply rewrite the active map id made the next position push
    look like a remap, which reset the trace and left the live segment growing
    into a continuous trace. The trace reply is applied while the position
    stream keeps ownership of the active map id.
    """
    state = _state_with_decoded_map("100")

    state = apply_command_data(
        state,
        "onMapTrace_V2",
        {"mid": "200", "batid": "fresh", "serial": "0", "type": "0", "index": 0,
         "info": "ignored"},
    )

    assert state.map.mid == "100"
    assert state.map.trace.batch_id == "fresh"


def test_trace_reply_for_active_map_is_applied() -> None:
    """A trace reply for the active map id is accepted and updates trace metadata."""
    state = _state_with_decoded_map("100")

    state = apply_command_data(
        state,
        "onMapTrace_V2",
        {"mid": "100", "batid": "fresh", "serial": "0", "type": "0", "index": 0,
         "info": "ignored"},
    )

    assert state.map.mid == "100"
    assert state.map.trace.batch_id == "fresh"


def test_base_map_reply_never_switches_active_map() -> None:
    """Only the position stream switches maps; geometry replies cannot flip it."""
    state = _state_with_decoded_map("100")

    state = apply_command_data(
        state,
        "onMapInfo_V2",
        {"mid": "200", "batid": "other", "serial": "0", "type": "0", "index": 0,
         "info": "ignored"},
    )

    assert state.map.mid == "100"


def test_o_series_rtk_station_position_parsed() -> None:
    """getRTK exposes the single fixed base station shown in place of beacons."""
    state = apply_command_data(
        MowerState(),
        "getRTK",
        {
            "result": 0,
            "rtks": [
                {"x": 1234, "y": 5678, "sn": "RTKSN0001", "state": 0, "mode": 0}
            ],
            "observations": {"solStat": 0, "roverSvs": 33},
        },
    )

    assert state.map.rtk_station is not None
    assert state.map.rtk_station.x == 1234
    assert state.map.rtk_station.y == 5678
    assert state.map.as_dict()["rtk_station"] == {
        "x": 1234,
        "y": 5678,
        "sn": "RTKSN0001",
    }


def test_o_series_rtk_empty_list_keeps_no_station() -> None:
    """An empty rtks list must not invent a station marker."""
    state = apply_command_data(MowerState(), "getRTK", {"result": 0, "rtks": []})
    assert state.map.rtk_station is None


def _make_subset(obj) -> str:
    """Build an O-series ``subsets`` blob (base64 + compact LZMA wrapper)."""
    import base64
    import json as _json
    import lzma

    raw = _json.dumps(obj, separators=(",", ":")).encode()
    comp = lzma.compress(
        raw,
        format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA1, "dict_size": 0x40000, "lc": 3, "lp": 0, "pb": 2}],
    )
    header = bytes([0x5D]) + (0x40000).to_bytes(4, "little") + len(raw).to_bytes(4, "little")
    return base64.b64encode(header + comp).decode()


def test_o_series_area_set_decodes_to_anchor_points() -> None:
    """getAreaSet 'ar' subsets decode (shared LZMA) to area anchor points."""
    subset = _make_subset([["1", "1", "Lawn", "", "100", "200", "0-0"]])
    state = apply_command_data(
        MowerState(),
        "getAreaSet",
        {"mid": "1", "aid": "0", "type": "ar", "subsets": subset, "infoSize": 1},
    )

    assert [p.as_dict() for p in state.map.areas] == [{"x": 100, "y": 200}]
    assert state.map.mid == "1"


def test_o_series_empty_virtual_walls_decode_to_no_zones() -> None:
    """The real captured empty 'vw' subset decodes to no no-go zones."""
    state = apply_command_data(
        MowerState(),
        "getMapTrack",
        {
            "mid": "1",
            "aid": "0",
            "type": "vw",
            "subsets": "XQAABAACAAAAAC2XPAAAAA==",
            "infoSize": 2,
        },
    )

    assert state.map.no_go_zones == ()
    assert state.map.mid == "1"


def test_o_series_virtual_wall_polygon_best_effort_decode() -> None:
    """A 'vw' record carrying a coordinate string yields a no-go polygon."""
    subset = _make_subset([["1", "1", "", "", "3;10,20;30,40;50,60"]])
    state = apply_command_data(
        MowerState(), "getMapTrack", {"mid": "1", "type": "vw", "subsets": subset}
    )

    assert len(state.map.no_go_zones) == 1
    assert [p.as_dict() for p in state.map.no_go_zones[0]] == [
        {"x": 10, "y": 20},
        {"x": 30, "y": 40},
        {"x": 50, "y": 60},
    ]


def test_o_series_map_state_learns_mid_without_decoding() -> None:
    """O-series map payloads only contribute the map id, never bogus geometry."""
    state = apply_command_data(
        MowerState(),
        "getMapTrack",
        {"mid": "987654", "totalCount": 400, "value": "<binary-blob>"},
    )

    assert state.map.mid == "987654"
    assert state.map.info.outline == ()
    assert state.map.trace.path == ()


def test_a_chunked_map_track_push_is_assembled_before_decoding() -> None:
    """getMapTrack answers with the full plan, split into chunks when large.

    Observed live 2026-08-31: a mowing plan (~7 kB, 180+ lane fields) always
    ships as serial="2" with index 0 and 1 under one batid — and the handler
    dropped anything it could not decode from a single message, so the plan
    never appeared for a mow while the small single-chunk trim loop worked.
    The chunks join as base64 BEFORE the LZMA decode.
    """
    record = [
        "1",
        "1",
        "1;1;7;0,0;-100,0,-100,400",
        "1;1;8;0,0;-200,0,-200,400",
        "1;2;0;-300,0;2(8)44(8)66(8)8",
    ]
    whole = _make_subset(record and [record])
    half = len(whole) // 2
    parts = [whole[:half], whole[half:]]

    state = MowerState()
    for index, info in enumerate(parts):
        state = apply_command_data(
            state,
            "onMapTrack",
            {
                "mid": "0",
                "batid": "abc123",
                "serial": "2",
                "index": str(index),
                "info": info,
            },
        )
        if index == 0:
            # Half a plan decodes to nothing: buffered, not applied.
            assert state.map.trace.lanes == {}
            assert state.map.trace.chunks

    assert set(state.map.trace.lanes) == {"7", "8"}
    assert state.map.trace.border is not None
    assert state.map.trace.chunks == {}


def test_chunks_from_different_batches_do_not_mix() -> None:
    """A new batid restarts assembly instead of joining unrelated parts."""
    record = [["1", "1", "1;1;5;0,0;-100,0,-100,400"]]
    whole = _make_subset(record)
    half = len(whole) // 2

    state = MowerState()
    state = apply_command_data(
        state,
        "onMapTrack",
        {"batid": "old", "serial": "2", "index": "0", "info": whole[:half]},
    )
    # The second half never arrives; a fresh batch starts over.
    state = apply_command_data(
        state,
        "onMapTrack",
        {"batid": "new", "serial": "2", "index": "0", "info": whole[:half]},
    )
    assert state.map.trace.lanes == {}
    state = apply_command_data(
        state,
        "onMapTrack",
        {"batid": "new", "serial": "2", "index": "1", "info": whole[half:]},
    )
    assert set(state.map.trace.lanes) == {"5"}


def test_an_idle_clean_push_does_not_cancel_a_return_to_dock() -> None:
    """A stopped job's ride home reports cleanState "idle" the whole way.

    Observed 2026-09-01: eight seconds into the return the idle push flipped
    the entity to IDLE and the tile said "ready — send it to the dock" while
    the mower was already driving there. Only the charge state may resolve a
    RETURNING: isCharging=1 lands it as DOCKED.
    """
    returning = MowerState(activity=MowerActivity.RETURNING)

    idle_push = '{"body": {"data": {"state": "idle"}}}'
    state = apply_mqtt_payload(
        returning, "iot/atr/onCleanInfo/x/y/z/j", idle_push
    )
    assert state.activity is MowerActivity.RETURNING

    docked_push = '{"body": {"data": {"isCharging": 1, "mode": "slotCharging"}}}'
    state = apply_mqtt_payload(
        state, "iot/atr/onChargeState/x/y/z/j", docked_push
    )
    assert state.activity is MowerActivity.DOCKED


def test_last_time_stats_never_moves_the_current_task_id() -> None:
    """Those stats describe the job that FINISHED, not the running one.

    Observed 2026-09-02: right after a recharge resume, onLastTimeStats
    carried cid=-539017078 (and a grouped refresh carried cid=0) while the
    real job was cid=122. Taken as the current task id it read as "a new task
    started", the coordinator wiped the remaining-work plan, and the mower cut
    for minutes with no lanes drawn.
    """
    state = MowerState(task_id="122")

    for payload in (
        '{"body": {"data": {"cid": -539017078, "area": 120, "time": 3600}}}',
        '{"body": {"data": {"cid": 0}}}',
    ):
        state = apply_mqtt_payload(
            state, "iot/atr/onLastTimeStats/x/y/z/j", payload
        )
        assert state.task_id == "122"

    # The running job's own report still moves it.
    state = apply_mqtt_payload(
        state,
        "iot/atr/onCleanInfo/x/y/z/j",
        '{"body": {"data": {"state": "clean", "cleanState": '
        '{"motionState": "working", "content": {"type": "auto"}, "cid": 123}}}}',
    )
    assert state.task_id == "123"


def _ring_record():
    """A closed octagon ring chain — 8 corners survive the collinear collapse,
    enough for the closed-announcement size floor."""
    return ["1", "1", "1;2;0;0,0;4(6)3(3)2(6)1(3)8(6)7(3)6(6)5(3)"]


def test_a_ring_reannouncement_does_not_repaint_cut_ground() -> None:
    """The mower re-sends the full planned ring on reconnection mid-job.

    Observed live 2026-09-02 minutes after an HA restart: the whole ring
    flashed back green over a lap already two-thirds cut. The accumulated
    cut cells must survive the announcement and be rubbed back out.
    """
    state = apply_mqtt_payload(
        MowerState(),
        "iot/atr/onCleanInfo/x/y/z/j",
        '{"body": {"data": {"state": "clean", "cleanState": '
        '{"motionState": "working", "content": {"type": "auto"}, "cid": 5}}}}',
    )
    state = apply_command_data(
        state, "onMapTrack", {"mid": "1", "info": _make_subset([_ring_record()])}
    )
    ring = state.map.trace.border
    assert state.map.trace.border_template is not None
    pelny = sum(len(s) for s in ring)

    # kosiarka melduje skoszenie kawalka wschodniego boku
    state = apply_command_data(
        state,
        "onMapTrack",
        {"mid": "1", "info": _make_subset([["1", "2", "1;2;0;150,0;4(2)"]])},
    )
    assert state.map.trace.border_cut
    po_cieciu = state.map.trace.border
    assert sum(len(s) for s in po_cieciu) != pelny or len(po_cieciu) > len(ring)
    def _ma_dziure(border):
        pkt = [p for s in border for p in s]
        return not any(p.y == 0 and 100 <= p.x < 300 for p in pkt)
    assert _ma_dziure(po_cieciu)

    # ponowne ogloszenie TEGO SAMEGO pierscienia — dziura ma zostac
    state = apply_command_data(
        state, "onMapTrack", {"mid": "1", "info": _make_subset([_ring_record()])}
    )
    assert _ma_dziure(state.map.trace.border)


def test_consecutive_cut_updates_erase_the_lap_between_them() -> None:
    """The updates are a few cells every couple of seconds while the mower
    drives further, so erasing only what they name left a sliver between
    every two of them — the ring came out dashed all round (observed live
    2026-09-04, in-mow edge pass). The lap between one update and the next
    was driven, so it goes too; the front of the last update is remembered
    across pushes for that.
    """
    state = apply_mqtt_payload(
        MowerState(),
        "iot/atr/onCleanInfo/x/y/z/j",
        '{"body": {"data": {"state": "clean", "cleanState": '
        '{"motionState": "working", "content": {"type": "auto"}, "cid": 5}}}}',
    )
    state = apply_command_data(
        state, "onMapTrack", {"mid": "1", "info": _make_subset([_ring_record()])}
    )
    # dwie aktualizacje na gornym boku, z przerwa 150 miedzy nimi
    state = apply_command_data(
        state,
        "onMapTrack",
        {"mid": "1", "info": _make_subset([["1", "2", "1;2;0;50,0;4"]])},
    )
    assert state.map.trace.border_cut_front == MapPosition(x=100, y=0)
    state = apply_command_data(
        state,
        "onMapTrack",
        {"mid": "1", "info": _make_subset([["1", "2", "1;2;0;250,0;4"]])},
    )
    assert state.map.trace.border_cut_front == MapPosition(x=300, y=0)
    pkt = [p for s in state.map.trace.border for p in s]
    # caly gorny bok zniknal, bez drzazgi miedzy aktualizacjami
    assert not any(p.y == 0 and 0 < p.x < 300 for p in pkt)
    # reszta pierscienia stoi
    assert any(p.x == -150 for p in pkt)


def test_a_one_cell_cut_update_still_says_where_the_mower_is() -> None:
    """Between snapshots the mower sometimes reports a single cell (anchor,
    no chain); it carries no shape but it is a point on the trail."""
    state = apply_mqtt_payload(
        MowerState(),
        "iot/atr/onCleanInfo/x/y/z/j",
        '{"body": {"data": {"state": "clean", "cleanState": '
        '{"motionState": "working", "content": {"type": "auto"}, "cid": 5}}}}',
    )
    state = apply_command_data(
        state, "onMapTrack", {"mid": "1", "info": _make_subset([_ring_record()])}
    )
    state = apply_command_data(
        state,
        "onMapTrack",
        {"mid": "1", "info": _make_subset([["1", "2", "1;2;0;150,0;"]])},
    )
    assert state.map.trace.border_cut_front == MapPosition(x=150, y=0)
    pkt = [p for s in state.map.trace.border for p in s]
    assert not any(p.y == 0 and 100 <= p.x <= 200 for p in pkt)


def test_a_late_ring_announcement_after_the_job_closed_is_ignored() -> None:
    """Once the lap is marked done (border == ()) with no job running, an
    archive re-announcement must not repaint it."""
    from dataclasses import replace as _replace

    state = MowerState()
    state = _replace(
        state,
        clean_type=None,
        map=_replace(
            state.map, trace=_replace(state.map.trace, border=())
        ),
    )
    state = apply_command_data(
        state, "onMapTrack", {"mid": "1", "info": _make_subset([_ring_record()])}
    )
    assert state.map.trace.border == ()


def test_job_plan_completed_only_on_a_real_job_exit() -> None:
    from dataclasses import replace as _replace

    from custom_components.ecovacs_goat.mower_messages import job_plan_completed
    from custom_components.ecovacs_goat.mower_models import MowerActivity

    def stan(activity, clean_type):
        return _replace(MowerState(), activity=activity, clean_type=clean_type)

    kosi = stan(MowerActivity.MOWING, "auto")
    # naturalny koniec: schodzi z koszenia, typ zadania juz pusty
    assert job_plan_completed(kosi, stan(MowerActivity.IDLE, None))
    assert job_plan_completed(kosi, stan(MowerActivity.RETURNING, None))
    # pauza w trakcie: to nie koniec
    assert not job_plan_completed(kosi, stan(MowerActivity.PAUSED, "auto"))
    # przerwa na ladowanie: typ zadania trwa, planu nie wolno kasowac
    assert not job_plan_completed(
        stan(MowerActivity.PAUSED, "auto"), stan(MowerActivity.DOCKED, "auto")
    )
    assert not job_plan_completed(None, stan(MowerActivity.IDLE, None))


def test_work_complete_reports_the_ride_home_as_returning() -> None:
    """A job the mower finishes on its own is followed by its own drive back
    to the dock, announced only as trigger=workComplete + idle (observed
    2026-09-02: workComplete at 15:43:04, charge state 48 s later). The ride
    must show as RETURNING, and docking still resolves it.
    """
    state = apply_mqtt_payload(
        MowerState(),
        "iot/atr/onCleanInfo/x/y/z/j",
        '{"body": {"data": {"state": "clean", "cleanState": '
        '{"motionState": "working", "content": {"type": "borderrotate"}}}}}',
    )
    assert state.activity is MowerActivity.MOWING

    koniec = '{"body": {"data": {"trigger": "workComplete", "state": "idle"}}}'
    state = apply_mqtt_payload(state, "iot/atr/onCleanInfo/x/y/z/j", koniec)
    assert state.activity is MowerActivity.RETURNING
    # robot wysyla ten push dwukrotnie — drugi nie moze nic zepsuc
    state = apply_mqtt_payload(state, "iot/atr/onCleanInfo/x/y/z/j", koniec)
    assert state.activity is MowerActivity.RETURNING

    state = apply_mqtt_payload(
        state,
        "iot/atr/onChargeState/x/y/z/j",
        '{"body": {"data": {"isCharging": 1, "mode": "slot"}}}',
    )
    assert state.activity is MowerActivity.DOCKED

    # workComplete z robota juz stojacego w stacji nie wyciaga go ze stacji
    state = apply_mqtt_payload(state, "iot/atr/onCleanInfo/x/y/z/j", koniec)
    assert state.activity is MowerActivity.DOCKED
