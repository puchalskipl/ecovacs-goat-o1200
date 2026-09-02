"""Parse ECOVACS GOAT mower responses and MQTT pushes."""

from __future__ import annotations

import base64
import binascii
from dataclasses import replace
import json
import lzma
import re
from typing import Any

from .map_geometry import (
    CHAIN_STEP,
    OUTLINE_SOURCE_MOWER,
    border_coverage_cells,
    compose_border,
    cut_cells_from_points,
    erode_border,
    parse_track_record,
    obstacles_from_area_info,
    outline_from_map_info,
)
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

# onMapTrack record kinds: a full snapshot of the remaining plan, versus an
# update touching individual lanes (see _map_track_push).
TRACK_SNAPSHOT = "1"


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
                    clean_trigger=_clean_trigger(data, state.clean_trigger),
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
            # Deliberately ignored. These are the stats of the job that
            # FINISHED, and their cid names that job — not the running one.
            # Taking it as the current task id flipped task_id to a stale
            # (often negative or 0) value mid-job, which the coordinator read
            # as "a new task started" and wiped the whole remaining-work plan:
            # observed 2026-09-02 right after a recharge resume, the mower
            # then cut for minutes with no lanes drawn at all. Everything this
            # push could tell us about the current job comes from cleanInfo.
            pass
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
                    state = replace(
                        state,
                        map=_map_track_push(
                            state.map, data, job_kind=state.clean_type
                        ),
                    )
                else:
                    # Unknown reply shape (e.g. a bare acknowledgement): still
                    # learn the map id like other O-series map replies.
                    state = replace(state, map=_map_mid_only(state.map, data))
        case "getArI" | "onArI":
            # O-series obstacle shapes (chain-coded polygons the mower learned).
            if isinstance(data, dict):
                state = replace(state, map=_map_obstacles(state.map, data))
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
            # O-series (RTK) map dialect. The HTTP reply only acknowledges;
            # the geometry arrives over MQTT. ``onMI`` carries the lawn
            # outline (chain-coded) which the app fills as the lawn — while a
            # job runs it may be an empty placeholder, so an empty decode
            # never clears the stored outline.
            if isinstance(data, dict):
                state = replace(state, map=_map_base_outline(state.map, data))
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
                        # Store whole percent: 1% resolution is plenty for a
                        # consumable, and decimals only clutter the history graph.
                        lifespans[str(item["type"])] = round(left / total * 100)
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
        case "getOta" | "onOta":
            # Firmware/OTA state; ``ver`` is the installed firmware version.
            if isinstance(data, dict) and data.get("ver"):
                state = replace(state, firmware_version=str(data["ver"]))
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
            # The settings themselves come from getAnimProtect and friends,
            # so this reply must not overwrite them.
            # Pushes are partial: a reply carrying only some flags must not
            # clear the others, otherwise an active protection flaps back to
            # "nothing is blocking" every time such a payload arrives.
            if isinstance(data, dict):
                current = state.protections
                state = replace(
                    state,
                    protections=MowerProtections(
                        animal_active=_bool_or(
                            data.get("isAnimProtect"), current.animal_active
                        ),
                        rain_active=_bool_or(
                            data.get("isRainProtect"), current.rain_active
                        ),
                        rain_delay_active=_bool_or(
                            data.get("isRainDelay"), current.rain_delay_active
                        ),
                        emergency_stop=_bool_or(
                            data.get("isEStop"), current.emergency_stop
                        ),
                        locked=_bool_or(data.get("isLocked"), current.locked),
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
        if current is MowerActivity.RETURNING:
            # A stopped job's ride home: cleanState reports "idle" (no job)
            # for the whole return — the arrival is signalled by the charge
            # state, not here. Without this, an idle push cancelled RETURNING
            # eight seconds into the drive and the tile said "ready — send it
            # to the dock" while the mower was already on its way (observed
            # 2026-09-01, ~100 s of nonsense).
            return MowerActivity.RETURNING
        if trigger == "workComplete" and current in (
            MowerActivity.MOWING,
            MowerActivity.PAUSED,
        ):
            # A job the mower finished on its own: it always drives itself
            # home next, but says only "idle" for the whole ride (observed
            # 2026-09-02: workComplete at 15:43:04, charge state 48 s later).
            # Report the return so the tile does not spend that ride saying
            # "done — send it to the dock" about a mower already on its way.
            return MowerActivity.RETURNING
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


def _clean_trigger(data: dict[str, Any], current: str | None) -> str | None:
    """Return why the job is in its current state, or None once it is over.

    The mower reports ``trigger`` alongside the clean state: "lowBattery"
    when it parks to recharge mid-job, "continue" when it picks the job back
    up, "workComplete" when it is done. Pushes that carry no trigger keep the
    last one, because the reason outlives the individual message.
    """
    value = data.get("trigger")
    if value:
        return str(value)
    if data.get("state") == "idle":
        return None
    return current


def job_plan_completed(
    previous: MowerState | None, current: MowerState
) -> bool:
    """Return whether the mowing job just closed for good.

    True only when the mower leaves MOWING/PAUSED for a non-working state
    with no job type left. The remaining-work layer (lanes, border, cut
    cells) is then history and must be cleared — otherwise the last plan
    lingers and, worse, an end-of-job ring re-announcement repaints the whole
    lap green (observed live 2026-09-02 at the moment a mow finished).

    Deliberately NOT true for a mid-job recharge break: the job type stays
    set while the mower pauses to charge, and wiping the plan there is
    exactly the bug fixed earlier the same day.
    """
    if previous is None:
        return False
    working = (MowerActivity.MOWING, MowerActivity.PAUSED)
    return (
        previous.activity in working
        and current.activity not in working
        and current.clean_type is None
    )


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


def _as_int(value: Any, *, default: int) -> int:
    """Parse the mower's stringly-typed counters (``serial``/``index``)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decode_map_subset(value: Any) -> Any:
    """Decode an O-series ``subsets`` blob (base64 + compact LZMA) to JSON."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return _decode_lzma_json_chunks({0: value})
    except (binascii.Error, ValueError, lzma.LZMAError, json.JSONDecodeError):
        return None


def _map_track_push(
    current: MowerMap, data: dict[str, Any], *, job_kind: str | None = None
) -> MowerMap:
    """Apply an O-series ``onMapTrack`` push: what the mower still has to cut.

    Pushes come in two kinds, told apart by the record's second token:

    * ``"1"`` — a **full snapshot** of the remaining plan (70+ lanes). It is
      authoritative: lanes missing from it are finished, which is how the
      mower retires them (it does not report them empty).
    * ``"2"`` — an **update** to individual lanes as they shrink, or a lane
      with no coordinates meaning that one is done.

    This is the layer the official app hatches over the lawn and rubs out
    piece by piece; it is not a record of where the mower has driven.

    A ``getMapTrack`` request is answered with the FULL plan as one of these
    pushes, split into chunks when it outgrows a single message (``serial``
    is the chunk count, ``index`` the chunk number, ``batid`` ties the parts
    together; the LZMA stream only decodes once the base64 parts are joined
    in order). A mowing plan (~7 kB, 180+ lane fields) always ships in two
    chunks — dropping multi-chunk pushes was why the plan never appeared for
    a mow while the small single-chunk trim loop worked from day one.
    """
    info = data.get("info")
    serial = _as_int(data.get("serial"), default=1)
    if serial > 1:
        batid = str(data.get("batid") or "")
        index = _as_int(data.get("index"), default=0)
        chunks = (
            dict(current.trace.chunks)
            if current.trace.batch_id == batid
            else {}
        )
        if isinstance(info, str):
            chunks[index] = info
        if len(chunks) < serial:
            return replace(
                current,
                trace=replace(current.trace, batch_id=batid, chunks=chunks),
            )
        info = "".join(chunks[i] for i in sorted(chunks))
        current = replace(
            current,
            trace=replace(current.trace, batch_id=batid, chunks={}),
        )

    decoded = _decode_map_subset(info)
    if not isinstance(decoded, list):
        return current

    step = current.info.chain_step or CHAIN_STEP
    lanes = dict(current.trace.lanes)
    border = current.trace.border
    border_template = current.trace.border_template
    border_lap_start = current.trace.border_lap_start
    border_cut = current.trace.border_cut
    fresh_cut: list[MapPosition] = []
    touched = False
    for record in decoded:
        if not isinstance(record, list) or len(record) < 2:
            continue
        snapshot = record[1] == TRACK_SNAPSHOT
        seen: dict[str, tuple[tuple[MapPosition, ...], ...]] = {}
        seen_border: tuple[tuple[MapPosition, ...], ...] = ()
        for field in record[2:]:
            parsed = parse_track_record(field, step=step)
            if parsed is None:
                continue
            if parsed.is_chain:
                # For the border lap the mower sends two different things
                # under one id: the whole remaining loop in a snapshot, and
                # the cells it has just cut in the updates between them.
                # Only the snapshot describes work left to do — taking an
                # update as the loop makes the border flicker between the
                # full loop and a stub at the mower. But the updates are the
                # live progress signal: their cells are accumulated and
                # erased from every composed border below.
                if snapshot:
                    touched = True
                    seen_border = parsed.segments
                else:
                    for chain_segment in parsed.segments:
                        fresh_cut.extend(chain_segment)
                continue
            touched = True
            if parsed.segments:
                seen[parsed.lane_id] = parsed.segments
            else:
                lanes.pop(parsed.lane_id, None)
        if snapshot:
            # Authoritative: whatever is not in the snapshot is already cut,
            # including the edge lap once it stops being listed.
            if seen:
                lanes = seen
            # Mid-job the mower snapshots only the arc up to the loop's
            # origin; compose the never-transmitted tail from the closed
            # announcement so the drawn remainder covers the whole lap.
            # A standalone edge trim always begins its lap at the dock, so
            # the station pins the lap start exactly even when the first
            # open arc arrives late. The in-mow edge pass starts wherever
            # the lanes ended — no hint there, the arc front estimates it.
            if job_kind is None and border == ():
                # The job is closed and the lap marked done — a late ring
                # (re-)announcement is archive chatter, not new work. Taking
                # it would repaint the whole finished lap green.
                continue
            origin_hint = None
            if job_kind == "borderrotate" and current.charge_positions:
                origin_hint = current.charge_positions[0]
            border, border_template, border_lap_start = compose_border(
                border_template,
                border_lap_start,
                seen_border,
                step=step,
                previous=border,
                origin_hint=origin_hint,
            )
            # A closed (re-)announcement deliberately does NOT reset the
            # accumulated cut cells. The mower re-sends the full planned ring
            # on reconnection mid-job (observed live 2026-09-02 right after an
            # HA restart: the whole ring flashed back green over a lap already
            # two-thirds cut) — erosion below immediately rubs the cut cells
            # back out. A genuinely new job gets its cells cleared by the
            # coordinator's task-start wipe, not here.
        else:
            lanes.update(seen)
    if fresh_cut:
        touched = True
        border_cut = border_cut | cut_cells_from_points(fresh_cut, step=step)
    if border and border_cut:
        border = erode_border(border, border_cut, step=step)
    if touched and current.trace.border:
        # Ratchet: cells the previous border covered but this one does not
        # were cut in the meantime (the plan only ever shrinks), even when no
        # update named them. Without this, a mid-job ring re-announcement
        # resurrects the slivers between sparse updates.
        lost = border_coverage_cells(
            current.trace.border, step=step
        ) - border_coverage_cells(border or (), step=step)
        if lost:
            border_cut = border_cut | lost
    if not touched:
        return current

    return replace(
        current,
        trace=replace(
            current.trace,
            lanes=lanes,
            border=border,
            border_template=border_template,
            border_lap_start=border_lap_start,
            border_cut=border_cut,
            batch_id=data.get("batid") or current.trace.batch_id,
            serial=str(data.get("serial")) if data.get("serial") else current.trace.serial,
            info_size=_int(data.get("infoSize")) or current.trace.info_size,
        ),
    )

def _map_base_outline(current: MowerMap, data: dict[str, Any]) -> MowerMap:
    """Apply the mower's lawn outline (``onMI``) and learn the map id.

    The outline is the mower's own stored map — the shape the app fills in
    green. Replies without geometry (acknowledgements, mid-job placeholders)
    only update the map id.
    """
    new = _map_mid_only(current, data)
    decoded = _decode_map_subset(data.get("info"))
    # The payload also carries the bounding-box centre, which is what makes
    # the grid scale derivable instead of assumed.
    outline, chain_step = outline_from_map_info(decoded, data)
    if not outline:
        return new
    return replace(
        new,
        info=replace(
            new.info,
            outline=outline,
            outline_source=OUTLINE_SOURCE_MOWER,
            chain_step=chain_step,
        ),
    )


def _map_obstacles(current: MowerMap, data: dict[str, Any]) -> MowerMap:
    """Apply the mower's learned obstacle shapes (``onArI``) to the map.

    Obstacles are chain-coded polygons in the same coordinate frame as the
    outline and positions — the app paints them as holes in the lawn.
    """
    decoded = _decode_map_subset(data.get("info"))
    obstacles = obstacles_from_area_info(
        decoded, step=current.info.chain_step or CHAIN_STEP
    )
    if not obstacles:
        return current
    return replace(current, info=replace(current.info, obstacles=obstacles))


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


def _bool_or(value: Any, current: bool | None) -> bool | None:
    """Return the payload's boolean, or keep the current one when absent."""
    parsed = _bool(value)
    return current if parsed is None else parsed


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
    # Whole percent: the mower reports fractional progress several times a
    # second, and the decimals only add noise to the state history.
    for key in ("progress", "cleanProgress", "mowingProgress", "percent", "percentage"):
        value = _float(data.get(key))
        if value is not None:
            if 0 <= value <= 1:
                value *= 100
            return float(round(max(0, min(100, value))))
    if mowed_area is None or job_area is None or job_area <= 0:
        return None
    return float(round(max(0, min(100, mowed_area / job_area * 100))))


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
