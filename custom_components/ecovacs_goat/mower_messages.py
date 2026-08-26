"""Parse ECOVACS GOAT mower responses and MQTT pushes."""

from __future__ import annotations

import base64
import binascii
from dataclasses import replace
import json
import lzma
import re
from typing import Any

from .mower_models import (
    AreaParameter,
    MapPosition,
    MowerActivity,
    MowerMap,
    MowerMapInfo,
    MowerMapTrace,
    MowerProtections,
    MowerSettings,
    MowerState,
    MowerStats,
    MowerZone,
    NetworkInfo,
)

MOWING_EFFICIENCY_OPTIONS = ("quick", "delicate")
# Mowing speed levels differ per family: the G1 line reports 1/2, while
# O-series mowers report 4 = 0.5 m/s ("efficiency") and 7 = 0.35 m/s
# ("delicate"). Reads accept both; writes pick the family's levels from the
# capability profile.
MOWING_EFFICIENCY_BY_LEVEL = {1: "quick", 2: "delicate", 4: "quick", 7: "delicate"}
MOWING_EFFICIENCY_LEVELS = {"quick": 1, "delicate": 2}
MOWING_EFFICIENCY_LEVELS_O_SERIES = {"quick": 4, "delicate": 7}

OBSTACLE_AVOIDANCE_OPTIONS = ("short_grass", "general", "bumpy_tall_grass")
OBSTACLE_AVOIDANCE_BY_LEVEL = {
    1: "short_grass",
    2: "general",
    3: "bumpy_tall_grass",
}
OBSTACLE_AVOIDANCE_LEVELS = {
    value: key for key, value in OBSTACLE_AVOIDANCE_BY_LEVEL.items()
}
ERROR_DESCRIPTIONS = {
    0: "NoError: Robot is operational",
    100: "NoError: Robot is operational",
    422: "Weak signal, back to station",
    4200: "Robot not reachable",
    500: "Request Timeout",
}
RETURN_TO_STATION_ERROR_CODES = {422}
POSITION_HISTORY_ACTIVITIES = {
    MowerActivity.MOWING,
    MowerActivity.RETURNING,
}
# The live position/beacon stream reports the map the mower is physically in, so
# it is the single source of truth for the active map id. Only these commands
# may switch the active map (and reset stale geometry); base-map / trace replies
# merely contribute geometry for whichever map is already active.
_ACTIVE_MAP_ID_COMMANDS = {"getPos", "onPos", "getUWB", "onUWB"}

# O-series live track (onMapTrack pushes): cap the accumulated mowed path.
O_SERIES_TRACK_MAX_POINTS = 4000
# O-series zone boundaries (onArI): 8-direction chain code decoding. Both the
# direction mapping and the step size are provisional best-effort values until
# calibrated against the official app map; the raw chain code is kept on every
# zone so polygons can be re-derived without another capture.
ZONE_CHAIN_STEP = 500
ZONE_CHAIN_DIRECTIONS = {
    1: (0, 1),
    2: (1, 1),
    3: (1, 0),
    4: (1, -1),
    5: (0, -1),
    6: (-1, -1),
    7: (-1, 0),
    8: (-1, 1),
}
_ZONE_CHAIN_TOKEN = re.compile(r"(\d)(?:\((\d+)\))?")


def decode_payload(payload: str | bytes | bytearray | dict[str, Any]) -> dict[str, Any]:
    """Decode a JSON MQTT/HTTP payload into a dictionary."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode()
    return json.loads(payload)


def command_payload(data: Any) -> dict[str, Any]:
    """Return the app-style command envelope for an N-GIoT request."""
    return {
        "body": {"data": data},
        "header": {
            "pri": 2,
            "ts": None,
            "tzm": None,
            "ver": "0.0.22",
        },
    }


def normalise_time(value: str | None) -> str | None:
    """Normalise ECOVACS time strings such as 19:0 to 19:00."""
    if not value:
        return value
    hour, minute = str(value).split(":", 1)
    return f"{int(hour):02d}:{int(minute):02d}"


def body_data(message: dict[str, Any]) -> Any:
    """Extract body.data from a response/push payload."""
    body = message.get("body", message)
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def response_data(response: dict[str, Any]) -> Any:
    """Extract data from an N-GIoT response."""
    if response.get("ret") == "ok" and "resp" in response:
        return body_data(decode_payload(response["resp"]))
    return body_data(response)


def apply_response(state: MowerState, command: str, response: dict[str, Any]) -> MowerState:
    """Apply an HTTP command response to cached state."""
    data = response_data(response)
    return apply_command_data(state, command, data)


def apply_mqtt_payload(state: MowerState, topic: str, payload: str | bytes | bytearray) -> MowerState:
    """Apply an MQTT push payload to cached state."""
    command = topic.split("/")[2] if "/" in topic else topic
    message = decode_payload(payload)
    data = body_data(message)
    if isinstance(data, dict):
        data = {**data, "_mqtt_ts": (message.get("header") or {}).get("ts")}
    return apply_command_data(state, command, data)


def apply_command_data(state: MowerState, command: str, data: Any) -> MowerState:
    """Apply command data from grouped reads, direct reads, or pushes."""
    if command == "getInfo" and isinstance(data, dict):
        for nested_command, nested in data.items():
            nested_data = nested.get("data", nested) if isinstance(nested, dict) else nested
            state = apply_command_data(state, nested_command, nested_data)
        return state

    if command in _ACTIVE_MAP_ID_COMMANDS:
        state = _reset_map_on_id_change(state, data)

    match command:
        case "getBattery" | "onBattery":
            if isinstance(data, dict):
                state = replace(state, battery=_int(data.get("value")))
        case "getChargeState" | "onChargeState":
            if isinstance(data, dict):
                state = replace(
                    state,
                    charging=_bool(data.get("isCharging")),
                    charge_mode=data.get("mode"),
                    activity=MowerActivity.DOCKED
                    if _bool(data.get("isCharging"))
                    else state.activity,
                )
        case "getCleanInfo_V2" | "onCleanInfo_V2" | "getCleanInfo" | "onCleanInfo":
            if isinstance(data, dict):
                activity = _clean_activity(data, state.activity)
                if (
                    activity is MowerActivity.PAUSED
                    and (
                        state.activity is MowerActivity.RETURNING
                        or state.error_code in RETURN_TO_STATION_ERROR_CODES
                    )
                ):
                    activity = MowerActivity.RETURNING
                mower_map = state.map
                if (
                    activity is MowerActivity.MOWING
                    and state.activity
                    not in {
                        MowerActivity.UNKNOWN,
                        MowerActivity.MOWING,
                        MowerActivity.PAUSED,
                    }
                ):
                    mower_map = replace(mower_map, position_history=())
                state = replace(
                    state,
                    activity=activity,
                    clean_type=_clean_content_type(data),
                    charging=False if activity is MowerActivity.MOWING else state.charging,
                    task_id=_task_id(data, state.task_id),
                    map=mower_map,
                )
        case "onWorkState" | "getWorkState":
            if isinstance(data, dict):
                activity = _work_state_activity(data, state.activity)
                mower_map = state.map
                if (
                    activity is MowerActivity.MOWING
                    and state.activity
                    not in {
                        MowerActivity.UNKNOWN,
                        MowerActivity.MOWING,
                        MowerActivity.PAUSED,
                    }
                ):
                    mower_map = replace(mower_map, position_history=())
                state = replace(
                    state,
                    activity=activity,
                    charging=False if activity is MowerActivity.MOWING else state.charging,
                    map=mower_map,
                )
        case "getStats" | "onStats" | "reportStats":
            if isinstance(data, dict):
                mowed_area = _int(data.get("mowedArea"))
                job_area = _int(data.get("area"))
                state = replace(
                    state,
                    task_id=_task_id(data, state.task_id),
                    stats=replace(
                        state.stats,
                        area=mowed_area if mowed_area is not None else job_area,
                        job_area=job_area,
                        progress=_progress(data, mowed_area, job_area),
                        duration=_int(data.get("time")),
                    ),
                )
        case "getLastTimeStats" | "onLastTimeStats":
            if isinstance(data, dict):
                state = replace(state, task_id=_task_id(data, state.task_id))
        case "getTotalStats":
            if isinstance(data, dict):
                state = replace(
                    state,
                    stats=replace(
                        state.stats,
                        total_area=_int(data.get("area")),
                        total_duration=_int(data.get("time")),
                        total_count=_int(data.get("count")),
                    ),
                )
        case "getError" | "onError":
            if isinstance(data, dict):
                codes = data.get("code")
                code = codes[-1] if isinstance(codes, list) and codes else _int(codes)
                state = replace(
                    state,
                    error_code=code,
                    error_description=ERROR_DESCRIPTIONS.get(code or 0),
                    activity=_error_activity(code, state),
                )
        case "getPos" | "onPos":
            if isinstance(data, dict):
                state = replace(
                    state,
                    map=_map_position_data(
                        state.map,
                        data,
                        record_history=state.activity in POSITION_HISTORY_ACTIVITIES,
                    ),
                )
        case "getUWB" | "onUWB":
            if isinstance(data, dict):
                state = replace(state, map=_map_uwb_data(state.map, data))
        case "getMapTrace_V2" | "onMapTrace_V2":
            if isinstance(data, dict):
                state = replace(state, map=_map_trace_data(state.map, data))
        case "getMapInfo_V2" | "onMapInfo_V2":
            if isinstance(data, dict):
                state = replace(state, map=_map_info_data(state.map, data))
        case "getMapTrack" | "onMapTrack" | "getAreaSet" | "onAreaSet":
            # O-series (RTK) map data arrives in two shapes: map-set layers
            # with a ``subsets`` blob (virtual walls "vw", areas "ar"), and
            # live track pushes with an ``info`` blob carrying a window of
            # recently mowed coordinates. Both use the shared compact-LZMA
            # wrapper.
            if isinstance(data, dict):
                if isinstance(data.get("subsets"), str):
                    state = replace(state, map=_map_set_layer(state.map, data))
                elif isinstance(data.get("info"), str):
                    state = replace(state, map=_map_track_push(state.map, data))
                else:
                    # Unknown reply shape (e.g. a bare acknowledgement): still
                    # learn the map id like other O-series map replies.
                    state = replace(state, map=_map_mid_only(state.map, data))
        case "getArI" | "onArI":
            # O-series zone boundaries: chain-coded polygons per mowing zone.
            if isinstance(data, dict):
                state = replace(state, map=_map_zones(state.map, data))
        case "getAreaParameter" | "onAreaParameter":
            # O-series per-zone mowing parameters (cutting height level, cut
            # mode, obstacle height, per-zone cut direction).
            if isinstance(data, dict):
                parameters = _area_parameters(data.get("areaParameters"))
                if parameters:
                    settings = replace(state.settings, area_parameters=parameters)
                    # O-series mowers manage obstacle avoidance per zone, so
                    # keep the shared setting in sync with the first zone.
                    obstacle_height = parameters[0].obstacle_height
                    if obstacle_height is not None:
                        settings = replace(
                            settings,
                            obstacle_avoidance=OBSTACLE_AVOIDANCE_BY_LEVEL.get(
                                obstacle_height
                            ),
                        )
                    state = replace(state, settings=settings)
        case "onFwBuryPoint-bd_batterytemp":
            if isinstance(data, dict):
                state = replace(
                    state,
                    telemetry=replace(
                        state.telemetry,
                        battery_temperature=_int(data.get("temperature")),
                    ),
                )
        case "onFwBuryPoint-bd_batteryinfo":
            if isinstance(data, dict):
                state = replace(
                    state,
                    telemetry=replace(
                        state.telemetry,
                        battery_level=_int(data.get("batteryLevel")),
                        battery_current=_int(data.get("batteryCurrent")),
                        battery_voltage=_int(data.get("batteryVoltage")),
                    ),
                )
        case "onFwBuryPoint-bd_power":
            if isinstance(data, dict):
                state = replace(
                    state,
                    telemetry=replace(
                        state.telemetry,
                        system_voltage=_int(data.get("systemVoltage")),
                        motor_voltage=_int(data.get("motorVoltage")),
                        motor_drive_voltage=_int(data.get("motorDriveVoltage")),
                        core_plate_voltage=_int(data.get("corePlateVoltage")),
                    ),
                )
        case (
            "getMapState"
            | "onMapState"
            | "getMI"
            | "onMI"
            | "getSpecialContour"
            | "onSpecialContour"
            | "getMapInfo"
            | "onMapInfo"
        ):
            # O-series (RTK) map dialect. The base-map / contour geometry for
            # these is delivered over MQTT, not in the HTTP reply, so we only
            # learn the map id here; the live marker comes from getPos/onPos
            # (deebotPos + rtkPos).
            if isinstance(data, dict):
                state = replace(state, map=_map_mid_only(state.map, data))
        case "getRTK" | "onRTK":
            # O-series RTK reference: the fixed base station position. There is
            # one station; show it where the G1 shows UWB beacons.
            if isinstance(data, dict):
                station = _rtk_station(data)
                if station is not None:
                    state = replace(
                        state, map=replace(state.map, rtk_station=station)
                    )
        case "getWifiList" | "onWifiList":
            if isinstance(data, dict):
                first = next(iter(data.get("list", []) or []), {})
                state = replace(
                    state,
                    network=NetworkInfo(
                        ip=first.get("ip"),
                        ssid=first.get("ssid"),
                        rssi=_int(first.get("rssi")),
                        mac=data.get("mac"),
                    ),
                )
        case "getLifeSpan":
            if isinstance(data, list):
                lifespans = dict(state.lifespans)
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    left = _float(item.get("left"))
                    total = _float(item.get("total"))
                    if item.get("type") and left is not None and total and total > 0:
                        lifespans[str(item["type"])] = round(left / total * 100, 2)
                state = replace(state, lifespans=lifespans)
        case "getRainDelay" | "onRainDelay":
            if isinstance(data, dict):
                state = replace(
                    state,
                    settings=replace(
                        state.settings,
                        rain_enabled=_bool(data.get("enable")),
                        rain_delay=_int(data.get("delay")),
                    ),
                )
        case "getAnimProtect" | "onAnimProtect":
            if isinstance(data, dict):
                state = replace(
                    state,
                    settings=replace(
                        state.settings,
                        animal_enabled=_bool(data.get("enable")),
                        animal_start=normalise_time(data.get("start")),
                        animal_end=normalise_time(data.get("end")),
                    ),
                )
        case "getRecognization" | "onRecognization":
            if isinstance(data, dict):
                state = replace(
                    state,
                    settings=replace(
                        state.settings,
                        ai_recognition=_bool(data.get("state")),
                    ),
                )
        case "getBorderSwitch" | "onBorderSwitch":
            if isinstance(data, dict):
                state = replace(
                    state,
                    settings=replace(
                        state.settings,
                        border_switch=_bool(data.get("enable")),
                        border_mode=_int(data.get("mode")),
                    ),
                )
        case "getChildLock" | "onChildLock":
            if isinstance(data, dict):
                state = replace(
                    state,
                    settings=replace(state.settings, safer_mode=_bool(data.get("on"))),
                )
        case "getMoveupWarning" | "onMoveupWarning":
            if isinstance(data, dict):
                state = replace(
                    state,
                    settings=replace(
                        state.settings,
                        move_up_warning=_bool(data.get("enable")),
                    ),
                )
        case "getCrossMapBorderWarning" | "onCrossMapBorderWarning":
            if isinstance(data, dict):
                state = replace(
                    state,
                    settings=replace(
                        state.settings,
                        cross_map_border_warning=_bool(data.get("enable")),
                    ),
                )
        case "getCutDirection" | "onCutDirection":
            if isinstance(data, dict):
                state = replace(
                    state,
                    settings=replace(
                        state.settings,
                        cut_direction=_int(data.get("angle")),
                    ),
                )
        case "getVolume" | "onVolume":
            # Speaker volumes, each out of ``total`` (0-10 on the O1200).
            if isinstance(data, dict):
                settings = state.settings
                state = replace(
                    state,
                    settings=replace(
                        settings,
                        volume=_int_or(data.get("volume"), settings.volume),
                        fall_volume=_int_or(
                            data.get("fallVolume"), settings.fall_volume
                        ),
                        search_volume=_int_or(
                            data.get("searchVolume"), settings.search_volume
                        ),
                        volume_total=_int_or(
                            data.get("total"), settings.volume_total
                        ),
                    ),
                )
        case "getAutoCutDirection" | "onAutoCutDirection":
            # Rotate the mowing direction automatically each week.
            if isinstance(data, dict):
                state = replace(
                    state,
                    settings=replace(
                        state.settings,
                        auto_cut_direction=_bool(data.get("enable")),
                    ),
                )
        case "getCutEfficiency" | "onCutEfficiency":
            if isinstance(data, dict):
                level = _int(data.get("level"))
                state = replace(
                    state,
                    settings=replace(
                        state.settings,
                        mowing_efficiency=MOWING_EFFICIENCY_BY_LEVEL.get(level or 0),
                    ),
                )
        case "getObstacleHeight" | "onObstacleHeight":
            if isinstance(data, dict):
                level = _int(data.get("level"))
                state = replace(
                    state,
                    settings=replace(
                        state.settings,
                        obstacle_avoidance=OBSTACLE_AVOIDANCE_BY_LEVEL.get(level or 0),
                    ),
                )
        case "onProtectState" | "getProtectState":
            # Runtime protection flags (``isAnimProtect``, ``isRainProtect``,
            # ``isLocked``, ...) report whether a protection is *active right
            # now*, not whether its setting is enabled: animal protection with
            # a 21:00-08:00 window reports 0 at midday while the setting is on.
            # The settings themselves come from getAnimProtect / getChildLock,
            # so this reply must not overwrite them.
            if isinstance(data, dict):
                state = replace(
                    state,
                    protections=MowerProtections(
                        animal_active=_bool(data.get("isAnimProtect")),
                        rain_active=_bool(data.get("isRainProtect")),
                        rain_delay_active=_bool(data.get("isRainDelay")),
                        emergency_stop=_bool(data.get("isEStop")),
                        locked=_bool(data.get("isLocked")),
                    ),
                )
        case "getRobotFeature" | "onRobotFeature":
            if isinstance(data, dict):
                state = replace(state, robot_features=dict(data))

    raw = dict(state.raw)
    raw[command] = data
    return replace(state, raw=raw, available=True)


def _clean_activity(data: dict[str, Any], current: MowerActivity) -> MowerActivity:
    state = data.get("state")
    clean_state = data.get("cleanState") or {}
    motion_state = clean_state.get("motionState")
    trigger = data.get("trigger")
    if trigger == "alert":
        return MowerActivity.ERROR
    if motion_state == "pause" or data.get("paused") == 1:
        return MowerActivity.PAUSED
    if state == "goCharging" or motion_state == "goCharging":
        return MowerActivity.RETURNING
    if state in ("clean", "working", "washing") or motion_state == "working":
        return MowerActivity.MOWING
    # A scheduled job that fires on the mower (not started from HA) reports a
    # schedule trigger; treat an active scheduled job as mowing even when the
    # exact state token differs by model (see issue #7, O1200 scheduled tasks).
    if trigger in ("schedule", "appointment", "scheduleClean") and state not in (
        "idle",
        "goCharging",
    ):
        return MowerActivity.MOWING
    if state == "idle":
        if current is MowerActivity.DOCKED:
            return MowerActivity.DOCKED
        return MowerActivity.IDLE
    return current


def _clean_content_type(data: dict[str, Any]) -> str | None:
    """Return the active job type from a cleanInfo payload.

    ``cleanState.content.type`` is "auto" for a full mow and "borderrotate"
    for edge trimming; the key is absent when no job is running.
    """
    clean_state = data.get("cleanState")
    if not isinstance(clean_state, dict):
        return None
    content = clean_state.get("content")
    if not isinstance(content, dict):
        return None
    value = content.get("type")
    return str(value) if value else None


def _task_id(data: dict[str, Any], current: str | None) -> str | None:
    """Return the best current mowing task id found in app payloads.

    O-series ``getCleanInfo`` nests the task id under ``cleanState.cid`` while G1
    stats readbacks expose it at the top level, so check both.
    """
    sources: list[dict[str, Any]] = [data]
    clean_state = data.get("cleanState")
    if isinstance(clean_state, dict):
        sources.append(clean_state)
    for source in sources:
        for key in ("bdTaskID", "mowid", "cid", "cleanId"):
            value = source.get(key)
            if value not in (None, ""):
                return str(value)
    return current


def _work_state_activity(data: dict[str, Any], current: MowerActivity) -> MowerActivity:
    robot_state = (data.get("robotState") or {}).get("state")
    station_state = (data.get("stationState") or {}).get("state")
    if data.get("paused") == 1:
        return MowerActivity.PAUSED
    if robot_state == "cleaning":
        return MowerActivity.MOWING
    if station_state in ("goCharging", "goEmptying"):
        return MowerActivity.RETURNING
    if station_state in ("charging", "emptying", "washing", "drying"):
        return MowerActivity.DOCKED
    if robot_state == "idle" and station_state == "idle":
        return MowerActivity.IDLE
    return current


def _error_activity(code: int | None, state: MowerState) -> MowerActivity:
    """Return activity implied by an error payload."""
    if code in (None, 0, 100):
        return state.activity
    if code in RETURN_TO_STATION_ERROR_CODES:
        if state.charging is True or state.activity is MowerActivity.DOCKED:
            return MowerActivity.DOCKED
        return MowerActivity.RETURNING
    return MowerActivity.ERROR


def _map_position_data(
    current: MowerMap, data: dict[str, Any], *, record_history: bool
) -> MowerMap:
    """Merge mower, station, and beacon positions into the map cache."""
    mower_position = _map_position(data.get("deebotPos"))
    # The O-series map origin IS the charging station: a docked mower reports
    # exactly (0, 0). Accept it as the marker position, learn it as the
    # station location (chargePos itself always comes back invalid), and keep
    # it out of the mowing trail so the path does not draw a line to the dock.
    at_dock = (
        mower_position is not None
        and mower_position.x == 0
        and mower_position.y == 0
    )
    charge_positions = _map_positions(data.get("chargePos"))
    if at_dock and not charge_positions and not current.charge_positions:
        charge_positions = (MapPosition(x=0, y=0),)
    # G1 reports UWB beacon positions; O-series (RTK) reports rtkPos instead.
    uwb_positions = _map_positions(data.get("uwbPos")) or _map_positions(
        data.get("rtkPos")
    )
    history = current.position_history

    if (
        record_history
        and mower_position
        and mower_position.invalid != 1
        and not at_dock
    ):
        if not history or (
            history[-1].x != mower_position.x or history[-1].y != mower_position.y
        ):
            history = (*history, mower_position)

    return replace(
        current,
        mid=_merge_stream_mid(current.mid, data.get("mid")),
        current_position=mower_position or current.current_position,
        charge_positions=charge_positions or current.charge_positions,
        uwb_positions=uwb_positions or current.uwb_positions,
        position_history=history,
        last_update_ts=_int(data.get("_mqtt_ts")) or current.last_update_ts,
        revision=current.revision + 1,
    )


def _merge_stream_mid(current_mid: str | None, incoming: Any) -> str | None:
    """Merge a position-stream map id, ignoring the O-series "0" placeholder."""
    if incoming is None:
        return current_mid
    incoming = str(incoming)
    if incoming == "0" and current_mid:
        return current_mid
    return incoming

def _map_uwb_data(current: MowerMap, data: dict[str, Any]) -> MowerMap:
    """Merge beacon position data from getUWB/onUWB payloads."""
    uwb_positions = _map_positions(data.get("uwbPos")) or _map_positions(
        data.get("rtkPos")
    )
    if uwb_positions and not any(
        position.x != 0 or position.y != 0 for position in uwb_positions
    ):
        uwb_positions = ()

    return replace(
        current,
        mid=_merge_stream_mid(current.mid, data.get("mid")),
        uwb_positions=uwb_positions or current.uwb_positions,
        last_update_ts=_int(data.get("_mqtt_ts")) or current.last_update_ts,
    )


def _map_trace_data(current: MowerMap, data: dict[str, Any]) -> MowerMap:
    """Merge chunked onMapTrace_V2 data into the map cache."""
    batch_id = str(data.get("batid")) if data.get("batid") is not None else None
    serial = str(data.get("serial")) if data.get("serial") is not None else None
    trace_type = str(data.get("type")) if data.get("type") is not None else None
    index = _int(data.get("index"))
    info = data.get("info")

    trace = current.trace
    if (
        batch_id
        and (
            trace.batch_id != batch_id
            or trace.serial != serial
            or trace.type != trace_type
        )
    ):
        trace = MowerMapTrace(batch_id=batch_id, serial=serial, type=trace_type)

    chunks = dict(trace.chunks)
    if index is not None and isinstance(info, str):
        chunks[index] = info
    path = _decode_trace_path(chunks) or trace.path

    return replace(
        current,
        # The active map id is owned solely by the live position stream
        # (:data:`_ACTIVE_MAP_ID_COMMANDS`). Trace replies only contribute
        # geometry for whichever map is already active; letting them change the
        # mid would make the next position push look like a remap and reset the
        # geometry we just decoded.
        mid=current.mid,
        trace=replace(
            trace,
            batch_id=batch_id or trace.batch_id,
            serial=serial or trace.serial,
            info_size=_int(data.get("infoSize")) or trace.info_size,
            type=trace_type or trace.type,
            chunks=chunks,
            path=path,
        ),
        last_update_ts=_int(data.get("_mqtt_ts")) or current.last_update_ts,
        revision=current.revision + 1,
    )


def _map_info_data(current: MowerMap, data: dict[str, Any]) -> MowerMap:
    """Merge chunked onMapInfo_V2 data into the base map cache."""
    batch_id = str(data.get("batid")) if data.get("batid") is not None else None
    serial = str(data.get("serial")) if data.get("serial") is not None else None
    map_type = str(data.get("type")) if data.get("type") is not None else None
    index = _int(data.get("index"))
    info = data.get("info")

    map_info = current.info
    if (
        batch_id
        and (
            map_info.batch_id != batch_id
            or map_info.serial != serial
            or map_info.type != map_type
        )
    ):
        map_info = MowerMapInfo(batch_id=batch_id, serial=serial, type=map_type)

    chunks = dict(map_info.chunks)
    if index is not None and isinstance(info, str):
        chunks[index] = info

    outline, obstacles = _decode_base_map(chunks)

    return replace(
        current,
        # See ``_map_trace_data``: base-map replies feed geometry but must never
        # re-own the active map id, which belongs to the live position stream.
        mid=current.mid,
        info=replace(
            map_info,
            batch_id=batch_id or map_info.batch_id,
            serial=serial or map_info.serial,
            info_size=_int(data.get("infoSize")) or map_info.info_size,
            type=map_type or map_info.type,
            chunks=chunks,
            outline=outline or map_info.outline,
            obstacles=obstacles or map_info.obstacles,
        ),
        last_update_ts=_int(data.get("_mqtt_ts")) or current.last_update_ts,
    )


def _reset_map_on_id_change(state: MowerState, data: Any) -> MowerState:
    """Drop stale map geometry when the active map id changes (remap).

    Resetting the mower and remapping produces a fresh ``mid``. The previously
    decoded base map outline, obstacles, live trace, mowed-area history, and
    charger/beacon positions all belong to the old map's coordinate frame, so
    keeping them would leave the integration showing the old base map while the
    mowed area drifts off it. Clearing the cached outline also lets the
    coordinator re-fetch ``getMapInfo_V2`` for the new map (it only requests the
    base map while no outline is cached). The live marker (``current_position``)
    is left untouched because it self-corrects from the next position push.

    This only runs for the authoritative live position/beacon stream
    (:data:`_ACTIVE_MAP_ID_COMMANDS`); base-map and trace replies never write
    the active map id (see ``_map_trace_data`` / ``_map_info_data``), so a
    geometry reply whose ``mid`` lives in a different namespace than the
    position stream cannot thrash the active map back and forth with the live
    stream or get spuriously discarded.
    """
    if not isinstance(data, dict):
        return state
    incoming = data.get("mid")
    if incoming is None:
        return state
    incoming = str(incoming)
    # O-series position pushes report mid "0" while map replies (onMI) carry
    # the real map id; "0" is a placeholder, not a map switch, and treating it
    # as one wipes the live geometry on every push after a map reply.
    if incoming == "0":
        return state
    current_mid = state.map.mid
    if not current_mid or current_mid == "0" or incoming == current_mid:
        return state
    return replace(
        state,
        map=replace(
            state.map,
            mid=incoming,
            info=MowerMapInfo(),
            trace=MowerMapTrace(),
            position_history=(),
            charge_positions=(),
            uwb_positions=(),
            revision=state.map.revision + 1,
        ),
    )


def _rtk_station(data: dict[str, Any]) -> MapPosition | None:
    """Return the RTK base station position from a getRTK/onRTK payload.

    The payload exposes ``rtks`` as a list, but an O-series setup has a single
    fixed base station, so the first valid entry is used.
    """
    stations = data.get("rtks")
    if not isinstance(stations, list):
        return None
    for item in stations:
        position = _map_position(item)
        if position is not None and position.invalid != 1:
            return position
    return None


def _map_mid_only(current: MowerMap, data: dict[str, Any]) -> MowerMap:
    """Record only the map id from an O-series map payload.

    The base-map and contour geometry for these replies arrives over MQTT (the
    HTTP reply only acknowledges), so we keep the existing geometry and just
    learn the current ``mid`` when present.
    """
    mid = data.get("mid")
    if mid is None or str(mid) == (current.mid or ""):
        return current
    return replace(current, mid=str(mid))


def _map_set_layer(current: MowerMap, data: dict[str, Any]) -> MowerMap:
    """Apply an O-series map-set layer (getMapTrack/getAreaSet).

    The payload is ``{mid, aid, type, subsets, infoSize}`` where ``subsets`` is a
    base64 + compact-LZMA blob (same wrapper as the G1 V2 map). ``type`` selects
    the layer: ``ar`` = mowing areas (anchor points), ``vw`` = virtual walls /
    no-go zones.
    """
    new = current
    mid = data.get("mid")
    if mid is not None and not current.mid:
        new = replace(new, mid=str(mid))

    decoded = _decode_map_subset(data.get("subsets"))
    if not isinstance(decoded, list):
        return new

    layer_type = data.get("type")
    if layer_type == "ar":
        new = replace(new, areas=_area_anchor_points(decoded))
    elif layer_type == "vw":
        new = replace(new, no_go_zones=_no_go_zone_polygons(decoded))
    return new


def _decode_map_subset(value: Any) -> Any:
    """Decode an O-series ``subsets`` blob (base64 + compact LZMA) to JSON."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return _decode_lzma_json_chunks({0: value})
    except (binascii.Error, ValueError, lzma.LZMAError, json.JSONDecodeError):
        return None


def _map_track_push(current: MowerMap, data: dict[str, Any]) -> MowerMap:
    """Accumulate an O-series live track push into the trace path.

    Each ``onMapTrack`` push carries a small LZMA window of recently mowed
    coordinates (records like ``["1", "2", "1;1;74;x,y;x,y;..."]`` where the
    leading semicolon tokens are header fields). The union of all pushes is the
    session's mowed path — the same layer the official app paints as "mowed".
    """
    decoded = _decode_map_subset(data.get("info"))
    if not isinstance(decoded, list):
        return current

    new_points: list[MapPosition] = []
    for record in decoded:
        if not isinstance(record, list):
            continue
        coordinates = next(
            (
                field
                for field in record
                if isinstance(field, str) and ";" in field and "," in field
            ),
            None,
        )
        if coordinates is None:
            continue
        for token in coordinates.split(";"):
            if "," not in token:
                continue
            x_text, y_text, *_rest = token.split(",")
            try:
                new_points.append(MapPosition(x=int(x_text), y=int(y_text)))
            except ValueError:
                continue
    if not new_points:
        return current

    seen = {(position.x, position.y) for position in current.trace.path}
    appended = list(current.trace.path)
    for position in new_points:
        key = (position.x, position.y)
        if key in seen:
            continue
        seen.add(key)
        appended.append(position)
    if len(appended) > O_SERIES_TRACK_MAX_POINTS:
        appended = appended[-O_SERIES_TRACK_MAX_POINTS:]

    return replace(
        current,
        trace=replace(
            current.trace,
            batch_id=str(data["batid"]) if data.get("batid") is not None else current.trace.batch_id,
            serial=str(data["serial"]) if data.get("serial") is not None else current.trace.serial,
            info_size=_int(data.get("infoSize")),
            path=tuple(appended),
        ),
    )


def _map_zones(current: MowerMap, data: dict[str, Any]) -> MowerMap:
    """Apply O-series zone boundaries (``onArI``) to the map.

    The decoded blob is a list of records; zone entries are strings shaped
    ``"<zone_id>;<anchor_x>,<anchor_y>;<chain code>"``.
    """
    decoded = _decode_map_subset(data.get("info"))
    if not isinstance(decoded, list):
        return current

    zones: list[MowerZone] = []
    for record in decoded:
        if not isinstance(record, list):
            continue
        for field in record:
            if not isinstance(field, str) or field.count(";") < 2:
                continue
            zone_id, anchor_text, chain = field.split(";", 2)
            if not zone_id.isdigit() or "," not in anchor_text:
                continue
            x_text, y_text, *_rest = anchor_text.split(",")
            try:
                anchor = MapPosition(x=int(x_text), y=int(y_text))
            except ValueError:
                continue
            zones.append(
                MowerZone(
                    zone_id=zone_id,
                    anchor=anchor,
                    boundary_code=chain,
                    polygon=_zone_polygon(anchor, chain),
                )
            )
    if not zones:
        return current
    return replace(current, zones=tuple(zones))


def _zone_polygon(anchor: MapPosition, chain: str) -> tuple[MapPosition, ...]:
    """Decode an 8-direction chain-coded zone boundary (best-effort).

    ``(n)`` suffixes repeat the preceding direction n extra times. Step size
    and direction mapping are provisional (``ZONE_CHAIN_STEP`` /
    ``ZONE_CHAIN_DIRECTIONS``) until calibrated against the app map.
    """
    x, y = anchor.x, anchor.y
    points = [MapPosition(x=x, y=y)]
    for match in _ZONE_CHAIN_TOKEN.finditer(chain):
        direction = ZONE_CHAIN_DIRECTIONS.get(int(match.group(1)))
        if direction is None:
            continue
        repeats = 1 + (int(match.group(2)) if match.group(2) else 0)
        for _ in range(repeats):
            x += direction[0] * ZONE_CHAIN_STEP
            y += direction[1] * ZONE_CHAIN_STEP
            points.append(MapPosition(x=x, y=y))
    if len(points) < 3:
        return ()
    return tuple(points)


def _area_parameters(value: Any) -> tuple[AreaParameter, ...]:
    """Parse ECOVACS ``areaParameters`` records into typed settings."""
    if not isinstance(value, list):
        return ()
    parameters: list[AreaParameter] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        area_id = _int(item.get("areaID"))
        if area_id is None:
            continue
        parameters.append(
            AreaParameter(
                area_id=area_id,
                mow_height_level=_int(item.get("mowHeightLevel")),
                cut_mode=_int(item.get("cutMode")),
                obstacle_height=_int(item.get("obstacleHeight")),
                angle=_int(item.get("angle")),
            )
        )
    return tuple(parameters)


def _area_anchor_points(records: list[Any]) -> tuple[MapPosition, ...]:
    """Return the anchor point of each ``ar`` area record.

    Captured shape: ``["<id>","<type>","<name>","","<x>","<y>","<code>"]``.
    """
    points: list[MapPosition] = []
    for record in records:
        if not isinstance(record, list) or len(record) < 6:
            continue
        try:
            points.append(MapPosition(x=int(record[4]), y=int(record[5])))
        except (TypeError, ValueError):
            continue
    return tuple(points)


def _no_go_zone_polygons(
    records: list[Any],
) -> tuple[tuple[MapPosition, ...], ...]:
    """Return virtual-wall polygons from ``vw`` records (best-effort).

    No populated virtual-wall capture is available yet, so we parse defensively:
    a record contributes a polygon when it carries a semicolon-delimited
    coordinate string; otherwise it is skipped.
    """
    zones: list[tuple[MapPosition, ...]] = []
    for record in records:
        if not isinstance(record, list):
            continue
        for field in record:
            if isinstance(field, str) and ";" in field and "," in field:
                polygon = _positions_from_coordinate_string(field)
                if len(polygon) >= 2:
                    zones.append(polygon)
                break
    return tuple(zones)


def _map_position(data: Any) -> MapPosition | None:
    if not isinstance(data, dict):
        return None
    return MapPosition.from_payload(data)


def _map_positions(data: Any) -> tuple[MapPosition, ...]:
    if not isinstance(data, list):
        return ()
    return tuple(
        position
        for item in data
        if (position := _map_position(item)) is not None and position.invalid != 1
    )


def _decode_trace_path(chunks: dict[int, str]) -> tuple[MapPosition, ...]:
    """Decode ECOVACS' chunked LZMA-wrapped live trace path."""
    try:
        payload = _decode_lzma_json_chunks(chunks)
    except (binascii.Error, ValueError, lzma.LZMAError, json.JSONDecodeError):
        return ()

    positions: list[MapPosition] = []
    if not isinstance(payload, list):
        return ()
    for item in payload:
        if not isinstance(item, list) or len(item) < 2 or not isinstance(item[1], str):
            continue
        for coordinates in item[1].split(";")[1:]:
            if "," not in coordinates:
                continue
            x_value, y_value, *_ = coordinates.split(",")
            try:
                positions.append(MapPosition(x=int(x_value), y=int(y_value)))
            except ValueError:
                continue
    return tuple(positions)


def _decode_base_map(
    chunks: dict[int, str],
) -> tuple[tuple[MapPosition, ...], tuple[tuple[MapPosition, ...], ...]]:
    """Decode ECOVACS' base map into lawn outline and obstacle polygons."""
    try:
        payload = _decode_lzma_json_chunks(chunks)
    except (binascii.Error, ValueError, lzma.LZMAError, json.JSONDecodeError):
        return (), ()

    if not isinstance(payload, list):
        return (), ()

    outline_candidates: list[tuple[MapPosition, ...]] = []
    obstacles: list[tuple[MapPosition, ...]] = []

    for item in payload:
        if not isinstance(item, list) or not item:
            continue
        layer = str(item[0])
        if layer in {"1", "2"} and len(item) > 1 and isinstance(item[1], str):
            positions = _positions_from_coordinate_string(item[1])
            if positions:
                outline_candidates.append(positions)
        elif layer == "3":
            for obstacle_data in item[1:]:
                if isinstance(obstacle_data, str):
                    obstacle = _positions_from_coordinate_string(obstacle_data)
                    if len(obstacle) >= 3:
                        obstacles.append(obstacle)

    outline = max(outline_candidates, key=len, default=())
    return outline, tuple(obstacles)


def _decode_lzma_json_chunks(chunks: dict[int, str]) -> Any:
    """Decode ECOVACS' compact LZMA chunk wrapper into JSON."""
    if not chunks:
        raise ValueError("No chunks")
    indexes = sorted(chunks)
    if indexes != list(range(indexes[-1] + 1)):
        raise ValueError("Incomplete chunks")
    raw = b"".join(base64.b64decode(chunks[index]) for index in indexes)
    if len(raw) < 10:
        raise ValueError("Chunk payload too small")
    props = raw[0]
    lc = props % 9
    remainder = props // 9
    lp = remainder % 5
    pb = remainder // 5
    decompressor = lzma.LZMADecompressor(
        format=lzma.FORMAT_RAW,
        filters=[
            {
                "id": lzma.FILTER_LZMA1,
                "dict_size": int.from_bytes(raw[1:5], "little"),
                "lc": lc,
                "lp": lp,
                "pb": pb,
            }
        ],
    )
    decoded = decompressor.decompress(
        raw[9:], max_length=int.from_bytes(raw[5:9], "little")
    )
    return json.loads(decoded)


def _positions_from_coordinate_string(value: str) -> tuple[MapPosition, ...]:
    """Parse semicolon-delimited ECOVACS map coordinates."""
    positions: list[MapPosition] = []
    for coordinates in value.split(";")[1:]:
        parts = coordinates.split(",")
        if len(parts) < 2:
            continue
        try:
            positions.append(MapPosition(x=int(parts[0]), y=int(parts[1])))
        except ValueError:
            continue
    return tuple(positions)


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(int(value)) if isinstance(value, str | int | float) else bool(value)


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _progress(
    data: dict[str, Any], mowed_area: int | None, job_area: int | None
) -> float | None:
    """Return current job mowing progress as a percentage."""
    for key in ("progress", "cleanProgress", "mowingProgress", "percent", "percentage"):
        value = _float(data.get(key))
        if value is not None:
            if 0 <= value <= 1:
                value *= 100
            return round(max(0, min(100, value)), 1)
    if mowed_area is None or job_area is None or job_area <= 0:
        return None
    return round(max(0, min(100, mowed_area / job_area * 100)), 1)


def merge_info_chunks(
    store: dict[str, dict[int, str]], data: Any
) -> dict[str, Any] | None:
    """Reassemble a chunked ``onInfo`` reply, returning it once complete.

    Grouped ``getInfo`` replies that exceed the MQTT payload limit are split
    into ``{d_id, d_seq, d_sum, d_val}`` fragments, each carrying a slice of
    the response JSON *as text*. Only the concatenation parses, so fragments
    are buffered per ``d_id`` until ``d_sum`` of them have arrived. Returns
    the decoded message (``{"body": {"data": {...}}}``) or None while
    incomplete.
    """
    if not isinstance(data, dict):
        return None
    batch_id = data.get("d_id")
    fragment = data.get("d_val")
    if batch_id is None or not isinstance(fragment, str):
        return None

    batch_id = str(batch_id)
    total = _int(data.get("d_sum")) or 1
    index = _int(data.get("d_seq")) or 0
    chunks = store.setdefault(batch_id, {})
    chunks[index] = fragment

    if len(chunks) < total:
        return None

    store.pop(batch_id, None)
    try:
        merged = json.loads("".join(chunks[key] for key in sorted(chunks)))
    except (json.JSONDecodeError, KeyError):
        return None
    return merged if isinstance(merged, dict) else None


def _int_or(value: Any, fallback: int | None) -> int | None:
    """Return ``value`` as an int, keeping ``fallback`` when it is absent."""
    parsed = _int(value)
    return fallback if parsed is None else parsed


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
