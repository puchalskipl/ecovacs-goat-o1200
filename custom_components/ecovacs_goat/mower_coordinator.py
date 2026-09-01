"""Coordinator for ECOVACS GOAT mower entities."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import replace
import logging
from math import atan2, degrees, hypot
from time import monotonic, time
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .debug_capture import DebugCaptureStore
from .goat_models import classify_goat_variant
from .map_geometry import (
    OUTLINE_SOURCE_COVERAGE,
    OUTLINE_SOURCE_MOWER,
    carry_forward_track,
    stabilise_geometry,
)
from .map_outline import outline_from_coverage, polygon_area
from .mower_profiles import MapDialect, profile_for_model
from .mower_compat import (
    ProtocolProfile,
    apply_resilient_getinfo_group,
    refresh_live_position,
    refresh_rtk_map,
)
from .mower_api import EcovacsApiError, EcovacsMowerApi
from .mower_messages import (
    MOWING_EFFICIENCY_LEVELS,
    OBSTACLE_AVOIDANCE_BY_LEVEL,
    OBSTACLE_AVOIDANCE_LEVELS,
    apply_command_data,
    apply_mqtt_payload,
    apply_response,
    body_data,
    decode_payload,
    merge_info_chunks,
)
from .mower_models import (
    AreaParameter,
    MapPosition,
    MowerActivity,
    MowerDevice,
    MowerLastJob,
    MowerMap,
    MowerMapInfo,
    MowerMapTrace,
    MowerState,
    standstill_bucket,
)
from .state_merge import changed_field_names, merge_refreshed_state
from .mower_mqtt import MowerAppPresenceMqttClient, MowerMqttClient

_LOGGER = logging.getLogger(__name__)
FRESH_STATE_SECONDS = 300
MAP_TRACE_TYPE = "0"
APP_LIVE_MAP_TYPES = ("ar", "vw", "fe")
MQTT_READBACK_DEBOUNCE_SECONDS = 3
# Pushes that carry new map geometry (lawn outline / obstacle shapes).
GEOMETRY_PUSH_COMMANDS = {"onMI", "onArI"}
# Push that carries the remaining-work lanes. Like the geometry above, only
# this may change them: a grouped refresh assembled moments earlier would
# otherwise publish whatever lanes were current then, and the layer would
# flicker between the two.
TRACK_PUSH_COMMANDS = {"onMapTrack"}
# Incomplete chunked-onInfo batches kept before assuming the rest are stale.
INFO_CHUNK_MAX_BATCHES = 8
# Availability watchdog: probe cadence and how long the mower may stay silent
# (no MQTT push, no successful readback) before a failed probe flips entities
# to unavailable. A docked mower is normally quiet, so the probe (not the
# silence alone) is what decides.
AVAILABILITY_CHECK_SECONDS = 300
AVAILABILITY_STALE_SECONDS = 900
MAP_TRACE_DIRECTION_THRESHOLD_DEGREES = 90
MAP_TRACE_POSITION_HEADING_MIN_DISTANCE = 20
LIVE_POSITION_SEGMENT_MAX_POINTS = 800
LIVE_POSITION_STREAM_REQUEST_MIN_INTERVAL_SECONDS = 60
APP_PING_INTERVAL_SECONDS = 120
APP_PRESENCE_MQTT_TTL_SECONDS = APP_PING_INTERVAL_SECONDS + 30
COMMAND_VERIFY_INITIAL_DELAY_SECONDS = 3
COMMAND_VERIFY_INTERVAL_SECONDS = 6
COMMAND_VERIFY_TIMEOUT_SECONDS = 90
RETURNING_REFRESH_SECONDS = 10
MOWING_POSITION_REFRESH_SECONDS = 60
# Rolling keepalive window for the auto live-map option: extended on every
# mowing refresh tick (60 s), so the app-style session stays open the whole
# job and expires within this window once mowing ends.
AUTO_LIVE_MAP_KEEPALIVE_SECONDS = 180
# cleanState.content.type of the edge-trimming job (captured from the app's
# border-cut mode on an O1200 LiDAR Pro).
EDGE_TRIM_CONTENT_TYPE = "borderrotate"
# Job kinds tracked for the "last mowing" / "last edge trim" summaries.
JOB_KIND_MOWING = "auto"
JOB_KIND_EDGE_TRIM = EDGE_TRIM_CONTENT_TYPE
# A job shorter than this with nothing mowed is an aborted start (e.g. a
# rain/animal-protection bounce), not a job worth remembering.
LAST_JOB_MIN_SECONDS = 60
# The border-cut job also needs the border region to cut. Without it the
# mower answers "get border content error" and aborts right after starting.
# The app sends "reid:1;" (region id 1 = the lawn's outer border).
EDGE_TRIM_CONTENT_VALUE = "reid:1;"
# Recompute the coverage outline after the track grows by this many points.
OUTLINE_RECOMPUTE_POINT_DELTA = 25
# Volume scale reported by getVolume when the mower has not answered yet.
VOLUME_DEFAULT_TOTAL = 10
# Settings field -> setVolume payload key.
VOLUME_PAYLOAD_KEYS = {
    "volume": "volume",
    "fall_volume": "fallVolume",
    "search_volume": "searchVolume",
}
POSITION_MQTT_STALE_SECONDS = 60
MAP_HISTORY_STORE_VERSION = 1
# Bumped whenever the map decoder changes shape/scale semantics: stored
# geometry from an older decoder is wrong on the new one, so it is dropped
# instead of being shown until the mower sends a fresh map.
MAP_GEOMETRY_VERSION = 3
MAP_HISTORY_STORE_DELAY_SECONDS = 5

# Polling policy:
# - Prefer MQTT pushes for normal state and live movement. In particular, onPos
#   should drive the live marker whenever ECOVACS publishes it.
# - Poll while mowing only as a gap-filler after onPos has been quiet for a
#   while; this avoids using frequent cloud reads as the animation source. That
#   poll also opens the trace gate and requests getMapTrace_V2 so the outline
#   stays current when MQTT never delivers heading deltas.
# - Poll while returning only until a terminal state is observed, because ECOVACS
#   may stop position pushes near the dock and HA still needs to notice docking.
# - Do not live-position poll for stable paused, stopped, idle, or docked states.
# - Gate mower-provided map trace pushes by accumulated heading change. Trace
#   payloads are heavier than onPos; accepting them after a turn keeps completed
#   mowing lines fresh without redrawing the full area on every trace push.
# - The fast app-style live-position stream is requested only through the
#   request_live_position_stream service. The custom card can start an explicit
#   keepalive window; background mowing refreshes stay at a slow getPos cadence.
ACTIONABLE_MQTT_READBACK_COMMANDS = {
    "onAnimProtect",
    "onAutoCutDirection",
    "onBorderSwitch",
    "onBreakPointStatus",
    "onChargeState",
    "onCleanInfo",
    "onCleanInfo_V2",
    "onCrossMapBorderWarning",
    "onCutDirection",
    "onCutEfficiency",
    "onError",
    "onMoveupWarning",
    "onObstacleHeight",
    "onProtectState",
    "onVolume",
    "onRainDelay",
    "onRecognization",
    "onStats",
    "onWorkState",
    "reportStats",
}
CUT_DIRECTION_LOCKED_ACTIVITIES = {
    MowerActivity.MOWING,
    MowerActivity.PAUSED,
    MowerActivity.RETURNING,
}
LIVE_POSITION_STREAM_ACTIVITIES = {
    MowerActivity.MOWING,
    MowerActivity.RETURNING,
}
COMMANDS_WITH_TASK_ID = {
    "charge",
    "clean_V2",
    "setAnimProtect",
    "setAreaParameter",
    "setAutoCutDirection",
    "setVolume",
    "setBorderSwitch",
    "setCrossMapBorderWarning",
    "setCutDirection",
    "setCutEfficiency",
    "setMoveupWarning",
    "setObstacleHeight",
    "setRainDelay",
    "setRecognization",
}

STARTUP_GET_INFO_GROUPS = (
    (
        "getUWB",
        "getMapState",
        "getChargeState",
        "getCleanInfo_V2",
        "getOta",
        "getRobotFeature",
    ),
    (
        "getBattery",
        "getBreakPointStatus",
        "getStats",
        "getError",
        "getLastTimeStats",
        "getMapUpdate",
        "getRelocationState",
    ),
    (
        "getProtectState",
        "getRecognization",
        "getNetworkSwitch",
        "getScheduleLatestTask",
        "getApnList",
        "getObstacleHeight",
        "getHumanoidWarning",
    ),
    (
        "getAnimProtect",
        "getCutEfficiency",
        "getBoundOpt",
        "getRainDelay",
        "getCutDirection",
        "getMoveupWarning",
        "getCrossMapBorderWarning",
        "getSleep",
        "getSafeProtect",
        "getRemoteSupport",
        "getGeolocation",
        "getBorderSwitch",
    ),
    # O-series reads kept in their own group: a mower that rejects one of them
    # can fail the whole batch without raising, which would silently blank the
    # established settings if they shared a group.
    (
        "getAreaParameter",
        "getAutoCutDirection",
        "getVolume",
    ),
)


def _mqtt_command(topic: str) -> str:
    """Return the command segment from an ECOVACS MQTT topic."""
    return topic.split("/")[2] if "/" in topic else topic


class MowerCoordinator(DataUpdateCoordinator[MowerState]):
    """Data coordinator for one mower."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: EcovacsMowerApi,
        device: MowerDevice,
        debug_capture: DebugCaptureStore | None = None,
        auto_live_map_fn: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"ECOVACS GOAT {device.did}",
        )
        self.api = api
        self.device = device
        self._debug_capture = debug_capture
        # Reads the config-entry option live, so toggling it takes effect
        # without a reload (checked on every mowing refresh tick).
        self._auto_live_map_fn = auto_live_map_fn
        self._capability = profile_for_model(device.model)
        self.data = self._base_state()
        self._protocol = ProtocolProfile(
            map_api_uses_v2=self._capability.map_uses_v2,
            get_pos_fields=self._capability.position_fields,
        )
        self._last_mqtt_at: float | None = None
        self._info_chunks: dict[str, dict[int, str]] = {}
        self._last_position_mqtt_at: float | None = None
        self._last_position_heading: float | None = None
        self._last_position_path_heading: float | None = None
        self._trace_heading_delta: float = 0
        self._trace_update_due = True
        self._last_readback_at: float | None = None
        self._mqtt_readback_task: asyncio.Task[None] | None = None
        self._outcome_refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._returning_refresh_task: asyncio.Task[None] | None = None
        self._mowing_position_refresh_task: asyncio.Task[None] | None = None
        self._trace_refresh_task: asyncio.Task[None] | None = None
        self._live_position_stream_task: asyncio.Task[None] | None = None
        self._live_position_keepalive_task: asyncio.Task[None] | None = None
        self._live_position_keepalive_until: float | None = None
        self._live_position_keepalive_reason = "keepalive"
        self._live_position_keepalive_force = False
        self._app_presence_stop_task: asyncio.Task[None] | None = None
        self._app_presence_stop_at: float | None = None
        # When the presence session was last cycled (see
        # _schedule_app_presence_cycle) — guards against restart storms.
        self._app_presence_cycled_at: float | None = None
        self._startup_live_map_task: asyncio.Task[None] | None = None
        self._availability_watchdog_task: asyncio.Task[None] | None = None
        self._last_live_position_stream_request_at: float | None = None
        self._live_map_request_counter = 0
        self._stop_unsub: Callable[[], None] | None = None
        self._stopped = False
        store_key = f"ecovacs_goat_map_history_{device.did}".replace("/", "_")
        self._map_history_store: Store[dict[str, Any]] = Store(
            hass, MAP_HISTORY_STORE_VERSION, store_key
        )
        self._saved_map_snapshot: tuple[Any, ...] | None = None
        # Newest map geometry the mower itself sent (onMI/onArI). Grouped
        # refreshes build their result from a snapshot taken seconds
        # earlier, so without this a slow refresh republishes the previous
        # outline and the map visibly flips back and forth.
        self._mower_geometry: MowerMapInfo | None = None
        # Set while publishing an onMI/onArI push, whose geometry is new
        # by definition and therefore replaces what we remembered.
        self._geometry_push_pending = False
        # Same idea for the remaining work (see TRACK_PUSH_COMMANDS). Lanes and
        # the border lap travel in the same push and must be remembered
        # together: the border is tri-state (None = no snapshot yet, () = done,
        # otherwise what is left), so it needs the surrounding tuple to tell
        # "remembered as None" from "nothing remembered yet".
        self._track_push_pending = False
        self._remembered_track: tuple[dict[str, tuple], tuple | None] | None = None
        # In-flight job being tracked for the last-job summaries.
        self._active_job: dict[str, Any] | None = None
        # Last-job records restored from storage before the first refresh.
        self._restored_last_jobs: dict[str, MowerLastJob] = {}
        # Learned setAreaParameter payload shape ("flat" or "wrapped").
        self._area_parameter_write_shape: str | None = None
        # Track length at the last coverage-outline computation.
        self._outline_source_points = 0
        self._mqtt = MowerMqttClient(
            api,
            device,
            hass.loop,
            self._handle_mqtt_message,
            debug_capture,
        )
        self._app_presence_mqtt = MowerAppPresenceMqttClient(
            api,
            device,
            hass.loop,
            debug_capture,
        )

    async def async_start(self) -> None:
        """Start push subscription after initial state refresh."""
        stored_map = await self._async_load_map_history()
        if stored_map is not None:
            self.data = replace(
                self.data,
                map=replace(
                    self.data.map,
                    mid=stored_map.mid or self.data.map.mid,
                    current_position=stored_map.current_position
                    or self.data.map.current_position,
                    position_history=stored_map.position_history,
                    trace=replace(
                        self.data.map.trace, lanes=stored_map.trace.lanes
                    ),
                    charge_positions=stored_map.charge_positions,
                    info=replace(
                        self.data.map.info,
                        outline=stored_map.info.outline
                        or self.data.map.info.outline,
                        obstacles=stored_map.info.obstacles
                        or self.data.map.info.obstacles,
                        outline_source=stored_map.info.outline_source
                        or self.data.map.info.outline_source,
                        # Without this the grid scale is lost on restart and
                        # obstacles arriving before the next onMI would decode
                        # at the fallback step instead of this map's own.
                        chain_step=stored_map.info.chain_step
                        or self.data.map.info.chain_step,
                    ),
                ),
            )
            self._saved_map_snapshot = (
                stored_map.position_history,
                stored_map.trace.lanes,
                stored_map.mid,
                stored_map.charge_positions,
                stored_map.current_position,
                stored_map.info.outline,
                stored_map.info.obstacles,
                stored_map.info.outline_source,
            )
        if self._restored_last_jobs:
            self.data = replace(
                self.data, last_jobs=dict(self._restored_last_jobs)
            )
        await self.async_config_entry_first_refresh()
        await self._mqtt.start()
        self._stop_unsub = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_handle_hass_stop
        )
        if self.data and self.data.activity is MowerActivity.MOWING:
            self._ensure_mowing_position_refresh()
        self._startup_live_map_task = self._create_background_task(
            self._async_refresh_live_map_after_mqtt_start(),
            "ecovacs_goat_startup_live_map",
        )
        self._availability_watchdog_task = self._create_background_task(
            self._async_availability_watchdog(),
            "ecovacs_goat_availability_watchdog",
        )

    async def async_stop(self) -> None:
        """Stop push subscription."""
        if self._stopped:
            return
        self._stopped = True
        if self._stop_unsub is not None and not self.hass.is_stopping:
            self._stop_unsub()
        self._stop_unsub = None
        if self._mqtt_readback_task and not self._mqtt_readback_task.done():
            self._mqtt_readback_task.cancel()
        if self._returning_refresh_task and not self._returning_refresh_task.done():
            self._returning_refresh_task.cancel()
        if (
            self._mowing_position_refresh_task
            and not self._mowing_position_refresh_task.done()
        ):
            self._mowing_position_refresh_task.cancel()
        if self._trace_refresh_task and not self._trace_refresh_task.done():
            self._trace_refresh_task.cancel()
        if (
            self._live_position_stream_task
            and not self._live_position_stream_task.done()
        ):
            self._live_position_stream_task.cancel()
        if (
            self._live_position_keepalive_task
            and not self._live_position_keepalive_task.done()
        ):
            self._live_position_keepalive_task.cancel()
        if self._app_presence_stop_task and not self._app_presence_stop_task.done():
            self._app_presence_stop_task.cancel()
        if self._startup_live_map_task and not self._startup_live_map_task.done():
            self._startup_live_map_task.cancel()
        if (
            self._availability_watchdog_task
            and not self._availability_watchdog_task.done()
        ):
            self._availability_watchdog_task.cancel()
        for task in self._outcome_refresh_tasks.values():
            task.cancel()
        await asyncio.gather(
            *(
                task
                for task in (
                    self._mqtt_readback_task,
                    self._returning_refresh_task,
                    self._mowing_position_refresh_task,
                    self._trace_refresh_task,
                    self._live_position_stream_task,
                    self._live_position_keepalive_task,
                    self._app_presence_stop_task,
                    self._startup_live_map_task,
                    self._availability_watchdog_task,
                    *self._outcome_refresh_tasks.values(),
                )
                if task
            ),
            return_exceptions=True,
        )
        self._availability_watchdog_task = None
        self._mqtt_readback_task = None
        self._returning_refresh_task = None
        self._mowing_position_refresh_task = None
        self._trace_refresh_task = None
        self._live_position_stream_task = None
        self._live_position_keepalive_task = None
        self._live_position_keepalive_until = None
        self._app_presence_stop_task = None
        self._app_presence_stop_at = None
        self._startup_live_map_task = None
        self._outcome_refresh_tasks.clear()
        await self._app_presence_mqtt.stop()
        await self._map_history_store.async_save(self._map_history_payload())
        await self._mqtt.stop()

    async def _async_handle_hass_stop(self, _event: Event) -> None:
        """Cancel background tasks early in Home Assistant shutdown."""
        self._stop_unsub = None
        await self.async_stop()

    @property
    def debug_capture(self) -> DebugCaptureStore:
        """Return the shared debug capture store."""
        assert self._debug_capture is not None
        return self._debug_capture

    async def _async_load_map_history(self) -> MowerMap | None:
        """Restore the persisted map geometry from HA storage."""
        stored = await self._map_history_store.async_load()
        if not isinstance(stored, dict):
            return None

        self._restored_last_jobs = {
            kind: job
            for kind, payload in (stored.get("last_jobs") or {}).items()
            for job in (MowerLastJob.from_payload(payload),)
            if job is not None
        }

        # A job that was still running when Home Assistant stopped: without
        # this the clock restarts on every restart and a three-hour session
        # gets recorded as however long the last leg happened to be.
        active = stored.get("active_job")
        if isinstance(active, dict) and active.get("started_at"):
            started = dt_util.parse_datetime(str(active["started_at"]))
            if started is not None:
                self._active_job = {
                    "kind": active.get("kind") or JOB_KIND_MOWING,
                    "started_at": started,
                    "task_id": active.get("task_id"),
                    "mowed_peak": float(active.get("mowed_peak") or 0.0),
                }

        def positions(value: Any) -> tuple[MapPosition, ...]:
            if not isinstance(value, list):
                return ()
            return tuple(
                position
                for item in value
                if isinstance(item, dict)
                for position in (MapPosition.from_payload(item),)
                if position is not None
            )

        # Geometry written by an older decoder no longer matches the map,
        # so it is discarded rather than drawn.
        geometry_current = stored.get("geometry_version") == MAP_GEOMETRY_VERSION
        obstacles = (
            tuple(
                shape
                for item in stored.get("obstacles", [])
                for shape in (positions(item),)
                if shape
            )
            if geometry_current
            else ()
        )
        mid = stored.get("mid")
        current = stored.get("current_position")
        return MowerMap(
            mid=str(mid) if mid else None,
            current_position=MapPosition.from_payload(current)
            if isinstance(current, dict)
            else None,
            position_history=positions(stored.get("position_history")),
            trace=MowerMapTrace(
                lanes={
                    str(lane_id): tuple(
                        positions(segment) for segment in segments if segment
                    )
                    for lane_id, segments in (
                        stored.get("trace_lanes") or {}
                    ).items()
                    if segments
                }
            ),
            charge_positions=positions(stored.get("charge_positions")),
            info=MowerMapInfo(
                outline=positions(stored.get("outline")) if geometry_current else (),
                obstacles=obstacles,
                outline_source=stored.get("outline_source")
                if geometry_current
                else None,
                chain_step=stored.get("chain_step") if geometry_current else None,
            ),
        )

    def _schedule_map_history_save(self, mower_map: MowerMap) -> None:
        """Debounce writes of the persisted map geometry to HA storage."""
        snapshot = (
            mower_map.position_history,
            mower_map.trace.lanes,
            mower_map.mid,
            mower_map.charge_positions,
            mower_map.current_position,
            mower_map.info.outline,
            mower_map.info.obstacles,
            mower_map.info.outline_source,
        )
        if snapshot == self._saved_map_snapshot:
            return
        self._saved_map_snapshot = snapshot
        self._map_history_store.async_delay_save(
            self._map_history_payload,
            MAP_HISTORY_STORE_DELAY_SECONDS,
        )

    def _map_history_payload(self) -> dict[str, Any]:
        """Return the persisted map history payload."""
        mower_map = self.data.map if self.data else MowerMap()
        return {
            "mid": mower_map.mid,
            "position_history": [
                position.as_dict() for position in mower_map.position_history
            ],
            "trace_lanes": {
                lane_id: [
                    [position.as_dict() for position in segment]
                    for segment in segments
                ]
                for lane_id, segments in mower_map.trace.lanes.items()
            },
            "obstacles": [
                [position.as_dict() for position in obstacle]
                for obstacle in mower_map.info.obstacles
            ],
            "charge_positions": [
                position.as_dict() for position in mower_map.charge_positions
            ],
            "current_position": mower_map.current_position.as_dict()
            if mower_map.current_position
            else None,
            "outline": [
                position.as_dict() for position in mower_map.info.outline
            ],
            "outline_source": mower_map.info.outline_source,
            "chain_step": mower_map.info.chain_step,
            "geometry_version": MAP_GEOMETRY_VERSION,
            "active_job": {
                **self._active_job,
                "started_at": self._active_job["started_at"].isoformat(),
            }
            if self._active_job
            else None,
            "last_jobs": {
                kind: job.as_dict()
                for kind, job in (
                    self.data.last_jobs if self.data else {}
                ).items()
            },
        }

    async def _async_update_data(self) -> MowerState:
        """Refresh from the mower using a small app-style command set."""
        base = self.data
        try:
            state = await self._async_refresh_state_groups()
            state = await self._async_refresh_extras(state)
        except EcovacsApiError as err:
            raise UpdateFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected ECOVACS update error: {err}") from err

        # Poll refreshes bypass async_set_updated_data, so merge and track
        # jobs here too — HA assigns this return value to self.data directly.
        merged = merge_refreshed_state(base, state, self.data)
        return self._track_job_lifecycle(self.data, merged)

    async def _async_refresh_extras(self, state: MowerState) -> MowerState:
        """Refresh network info, consumable lifespans, and total stats."""
        for command, payload in (
            ("getWifiList", {}),
            # The firmware tracks a single consumable: the blade. Explicit
            # type selectors ("-1", type lists) return the same one record,
            # so the app's cutting-line/brush counters must be cloud-side.
            ("getLifeSpan", {}),
            ("getTotalStats", {}),
        ):
            try:
                response = await self.api.control(self.device, command, payload)
                state = apply_response(state, command, response)
            except EcovacsApiError as err:
                _LOGGER.debug(
                    "ECOVACS coordinator update skipped %s: %s", command, err
                )
        return state

    def _handle_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Handle a pushed MQTT message from paho's thread."""
        def update_state() -> None:
            try:
                command = _mqtt_command(topic)
                now = monotonic()
                if command == "onMapTrace_V2" and not self._should_accept_trace_mqtt():
                    self._last_mqtt_at = now
                    self._capture_event(
                        "mqtt_trace_throttled",
                        {
                            "command": command,
                            "accumulated_heading_delta": round(
                                self._trace_heading_delta, 1
                            ),
                            "threshold_degrees": MAP_TRACE_DIRECTION_THRESHOLD_DEGREES,
                        },
                    )
                    return
                self._last_mqtt_at = now
                if command == "onInfo":
                    self._apply_info_chunk(payload)
                    return
                previous_state = self.data
                self._geometry_push_pending = command in GEOMETRY_PUSH_COMMANDS
                self._track_push_pending = command in TRACK_PUSH_COMMANDS
                state = apply_mqtt_payload(self.data, topic, payload)
                if command == "onPos":
                    self._last_position_mqtt_at = self._last_mqtt_at
                    self._update_trace_direction_gate(state)
                    state = self._compact_live_position_segment(state)
                if command == "onMapTrace_V2":
                    if self._trace_path_changed(previous_state, state):
                        state = self._reset_live_position_segment(state)
                        self._mark_trace_mqtt_applied()
                self.async_set_updated_data(state)
                self._capture_event(
                    "mqtt_parsed",
                    {
                        "command": command,
                        "activity": state.activity.value
                        if state.activity is not None
                        else None,
                        "map_revision": state.map.revision,
                        "handled": True,
                    },
                )
                if command in ACTIONABLE_MQTT_READBACK_COMMANDS:
                    self._schedule_mqtt_readback()
                if state.activity is MowerActivity.MOWING:
                    self._ensure_mowing_position_refresh()
                if state.activity is MowerActivity.RETURNING:
                    self._ensure_returning_refresh()
            except Exception as err:
                _LOGGER.exception("Failed to parse ECOVACS MQTT message %s", topic)
                self._capture_event(
                    "mqtt_parse_error",
                    {
                        "topic": topic,
                        "command": _mqtt_command(topic),
                        "payload_size": len(payload),
                        "exception": repr(err),
                    },
                )

        self.hass.loop.call_soon_threadsafe(update_state)

    def _apply_info_chunk(self, payload: bytes) -> None:
        """Buffer a chunked ``onInfo`` reply and apply it once complete.

        Grouped ``getInfo`` replies larger than the MQTT payload limit arrive
        split across ``onInfo`` fragments instead of the HTTP response, so
        without reassembly every setting in that group stays unknown.
        """
        message = decode_payload(payload)
        merged = merge_info_chunks(self._info_chunks, body_data(message))
        if merged is None:
            # Batches that lost a fragment would otherwise accumulate forever.
            # Complete batches are removed by merge_info_chunks; almost always
            # at most one batch is in flight, so hitting the cap means the
            # rest are stale partials — drop them all and let a resend win.
            if len(self._info_chunks) > INFO_CHUNK_MAX_BATCHES:
                self._info_chunks.clear()
            return
        state = apply_command_data(self.data, "getInfo", body_data(merged))
        self.async_set_updated_data(state)
        self._capture_event(
            "info_chunks_applied",
            {"commands": sorted(body_data(merged) or {})},
        )

    def _should_accept_trace_mqtt(self) -> bool:
        """Return whether an incoming mower trace push should update HA state."""
        if not self.data or not self.data.map.trace.path:
            return True
        return self._trace_update_due

    def _update_trace_direction_gate(self, state: MowerState) -> None:
        """Mark trace refresh due once stepped heading changes add up enough."""
        current = state.map.current_position
        if current is None or current.invalid == 1:
            return

        deltas: list[float] = []
        heading = current.a
        if heading is not None:
            current_heading = float(heading)
            if self._last_position_heading is not None:
                deltas.append(
                    abs(self._angle_delta(self._last_position_heading, current_heading))
                )
            self._last_position_heading = current_heading

        path_heading = self._path_heading_from_previous_position(current)
        if path_heading is not None:
            if self._last_position_path_heading is not None:
                deltas.append(
                    abs(
                        self._angle_delta(
                            self._last_position_path_heading, path_heading
                        )
                    )
                )
            self._last_position_path_heading = path_heading

        if not deltas:
            return

        self._trace_heading_delta += max(deltas)
        if self._trace_heading_delta >= MAP_TRACE_DIRECTION_THRESHOLD_DEGREES:
            self._trace_update_due = True
            self._schedule_trace_refresh()

    def _path_heading_from_previous_position(
        self, current: MapPosition
    ) -> float | None:
        """Return movement-derived heading for trace gating."""
        previous = self.data.map.current_position if self.data else None
        if previous is None or previous.invalid == 1:
            return None
        dx = current.x - previous.x
        dy = current.y - previous.y
        if hypot(dx, dy) < MAP_TRACE_POSITION_HEADING_MIN_DISTANCE:
            return None
        return degrees(atan2(dy, dx))

    def _mark_trace_mqtt_applied(self) -> None:
        """Reset the trace gate after applying a mower trace push."""
        self._trace_heading_delta = 0
        self._trace_update_due = False
        self._last_position_heading = None
        self._last_position_path_heading = None

    @staticmethod
    def _trace_path_changed(previous: MowerState | None, current: MowerState) -> bool:
        """Return whether the mower-provided trace path actually advanced."""
        if not current.map.trace.path:
            return False
        return previous is None or previous.map.trace.path != current.map.trace.path

    def async_set_updated_data(self, data: MowerState) -> None:
        """Publish new state; reset the track on new jobs and persist the map.

        O-series mowers accumulate the ``onMapTrack`` track across pushes; the
        track belongs to one mowing task, so a task-id change (a new job
        started from the app or schedule) resets it. Mid-job recharge resumes
        keep the same task id and therefore keep the track. The path, track,
        and zone geometry are then persisted (debounced) so the map survives
        Home Assistant restarts.
        """
        previous = self.data
        data = self._track_job_lifecycle(previous, data)
        if (
            previous is not None
            and previous.task_id is not None
            and data.task_id is not None
            and data.task_id != previous.task_id
            and data.map.trace.lanes
        ):
            data = replace(
                data,
                map=replace(
                    data.map,
                    trace=replace(
                        data.map.trace,
                        lanes={},
                        border=None,
                        border_template=None,
                        border_lap_start=None,
                    ),
                ),
            )
            self._remembered_track = ({}, None, None, None)
        data = self._carry_forward_track(previous, data)
        data = self._carry_forward_map_geometry(previous, data)
        data = self._maybe_update_outline(data)
        super().async_set_updated_data(data)
        self._schedule_map_history_save(data.map)

    def _carry_forward_track(
        self, previous: MowerState | None, data: MowerState
    ) -> MowerState:
        """Let only an onMapTrack push change the remaining work to be cut.

        Everything else that publishes state — grouped refreshes above all —
        was assembled before the newest push and carries an older track. Left
        alone it makes the layer flicker, which shows up as a boundary that
        alternates between "still to cut" and "done" on every redraw.

        Lanes and the border lap are carried together. Carrying only the lanes
        left the border blanked by every ordinary refresh, so during an edge
        trim the card kept redrawing whatever loop it had last seen instead of
        the one that is actually left.
        """
        remapped = (
            previous is not None
            and data.map.mid is not None
            and previous.map.mid != data.map.mid
        )
        from_push = self._track_push_pending
        self._track_push_pending = False
        incoming = (
            data.map.trace.lanes,
            data.map.trace.border,
            data.map.trace.border_template,
            data.map.trace.border_lap_start,
        )
        published, self._remembered_track = carry_forward_track(
            self._remembered_track,
            incoming,
            from_push=from_push,
            remapped=remapped,
        )
        if published == incoming:
            return data
        lanes, border, template, lap_start = published
        return replace(
            data,
            map=replace(
                data.map,
                trace=replace(
                    data.map.trace,
                    lanes=lanes,
                    border=border,
                    border_template=template,
                    border_lap_start=lap_start,
                ),
            ),
        )

    def _carry_forward_map_geometry(
        self, previous: MowerState | None, data: MowerState
    ) -> MowerState:
        """Publish the newest mower-sent map geometry (see stabilise_geometry)."""
        remapped = (
            previous is not None
            and data.map.mid is not None
            and previous.map.mid != data.map.mid
        )
        info, self._mower_geometry = stabilise_geometry(
            self._mower_geometry,
            data.map.info,
            learn=self._geometry_push_pending,
            remapped=remapped,
        )
        self._geometry_push_pending = False
        if info is data.map.info:
            return data
        return replace(data, map=replace(data.map, info=info))

    def _sample_job_standstill(self, job: dict[str, Any], data: MowerState) -> None:
        """Charge the time since the last sample to what is holding the job up.

        Sampled rather than event-driven: state is republished every few
        seconds while a job is open, which is far finer than the minutes this
        ends up reporting. ``blocked`` wins over ``charging`` when both hold —
        during a rain break the mower tops up its battery, but what the job is
        waiting for is the weather.
        """
        now = dt_util.utcnow()
        previous_sample = job.get("sampled_at") or now
        job["sampled_at"] = now
        elapsed = (now - previous_sample).total_seconds()
        if elapsed <= 0:
            return
        protections = data.protections
        bucket = standstill_bucket(
            mowing=data.activity is MowerActivity.MOWING,
            blocked=any(
                (
                    protections.rain_active,
                    protections.rain_delay_active,
                    protections.animal_active,
                    protections.emergency_stop,
                    protections.locked,
                )
            ),
            charging=bool(data.charging),
        )
        if bucket is not None:
            key = f"{bucket}_seconds"
            job[key] = job.get(key, 0.0) + elapsed

    def _track_job_lifecycle(
        self, previous: MowerState | None, data: MowerState
    ) -> MowerState:
        """Maintain the per-kind summaries of the most recent finished jobs.

        The mower's own ``getLastTimeStats`` keeps only the single latest task,
        without a timestamp, so the coordinator watches activity transitions
        itself: a job starts when the mower begins working and is recorded when
        it settles back on the dock. Legs of one task split by a mid-job
        recharge share a task id and are merged into one record.
        """
        if previous is not None and previous.last_jobs and not data.last_jobs:
            data = replace(data, last_jobs=dict(previous.last_jobs))

        job = self._active_job
        if data.activity in (
            MowerActivity.MOWING,
            MowerActivity.PAUSED,
            MowerActivity.RETURNING,
        ):
            if job is None and data.activity is MowerActivity.MOWING:
                job = self._active_job = {
                    "kind": data.clean_type or JOB_KIND_MOWING,
                    "started_at": dt_util.utcnow(),
                    "task_id": data.task_id,
                    "mowed_peak": 0.0,
                    "blocked_seconds": 0.0,
                    "charging_seconds": 0.0,
                    "sampled_at": dt_util.utcnow(),
                }
                # A fresh job needs a fresh app-presence CONNECT: the mower
                # broadcasts its plan (onMI/onArI + onMapTrack snapshots)
                # when it sees the app come online — the connect edge, not
                # the connected state. A session left open from before the
                # job produces no edge and the plan never arrives (observed:
                # a whole 78-minute mowing leg with zero snapshots, while
                # every edge trim — whose start coincided with a presence
                # reconnect — got its first snapshot within seconds of
                # app_presence_mqtt_connected).
                self._schedule_app_presence_cycle("job_started")
            if job is not None:
                if data.clean_type:
                    job["kind"] = data.clean_type
                if data.task_id:
                    job["task_id"] = data.task_id
                if data.stats.area:
                    job["mowed_peak"] = max(
                        job["mowed_peak"], data.stats.area / 10000
                    )
                self._sample_job_standstill(job, data)
            return data

        if job is None:
            return data
        if data.clean_type is not None:
            # The task is still open (cleanState carries a job type): what
            # looked like "docked" is the transient isCharging push racing the
            # clean-state push while the mower parks to recharge. Closing here
            # recorded a mid-job "last mowing" and fired the edge-trim
            # automation while the mower was still busy.
            self._sample_job_standstill(job, data)
            return data
        self._active_job = None
        ended_at = dt_util.utcnow()
        elapsed_seconds = (ended_at - job["started_at"]).total_seconds()
        if elapsed_seconds < LAST_JOB_MIN_SECONDS:
            # Aborted start (protection bounce, command error, a job stopped
            # seconds in) — not a job. The reported area cannot vouch for it:
            # the mower's session counter carries over from the previous run,
            # so a five-second edge trim was recorded with the full 15.7 m² of
            # the one before it (observed 2026-08-30) and then suppressed the
            # next scheduled trim for the whole interval. At the mower's
            # 0.35–0.5 m/s nothing meaningful is cut in under a minute anyway.
            return data

        kind = (
            JOB_KIND_EDGE_TRIM
            if job["kind"] == JOB_KIND_EDGE_TRIM
            else JOB_KIND_MOWING
        )
        started_at = job["started_at"]
        existing = data.last_jobs.get(kind)
        if (
            existing is not None
            and existing.task_id
            and existing.task_id == job["task_id"]
            and existing.started_at
        ):
            # A recharge split this task into legs: the job began at the first
            # of them, and the reported time covers everything since — the
            # break included, which is what "how long did it take" means.
            started_at = min(started_at, dt_util.parse_datetime(existing.started_at))
        # Legs of one task carry their own standstill; the record reports the
        # whole task, so they add up (unlike the area, which the mower already
        # reports cumulatively).
        merged_leg = (
            existing is not None
            and existing.task_id
            and existing.task_id == job["task_id"]
        )
        blocked = job.get("blocked_seconds", 0.0) / 60
        charging = job.get("charging_seconds", 0.0) / 60
        if merged_leg:
            blocked += existing.blocked_minutes or 0.0
            charging += existing.charging_minutes or 0.0
        record = MowerLastJob(
            kind=kind,
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            mowed_area=max(
                round(job["mowed_peak"], 1),
                (existing.mowed_area or 0.0) if existing else 0.0,
            ),
            duration_minutes=round(
                (ended_at - started_at).total_seconds() / 60, 1
            ),
            task_id=job["task_id"],
            blocked_minutes=round(blocked, 1),
            charging_minutes=round(charging, 1),
        )
        data = replace(data, last_jobs={**data.last_jobs, kind: record})
        self._map_history_store.async_delay_save(
            self._map_history_payload, MAP_HISTORY_STORE_DELAY_SECONDS
        )
        return data

    def _maybe_update_outline(self, data: MowerState) -> MowerState:
        """Derive a lawn outline from coverage when the mower sent none.

        The mower's own outline (``onMI``) is authoritative and is used as-is;
        this fallback only fills in for mowers/firmware that never send one,
        by tracing the boundary of the accumulated coverage. Recomputed only
        after meaningful growth to keep the work off the hot path.
        """
        if data.map.info.outline_source == OUTLINE_SOURCE_MOWER:
            return data
        # The onMapTrack windows are patchy, so combine them with the live
        # position history — both are places the mower actually drove.
        coverage = (*data.map.trace.path, *data.map.position_history)
        if (
            data.map.info.outline
            and abs(len(coverage) - self._outline_source_points)
            < OUTLINE_RECOMPUTE_POINT_DELTA
        ):
            return data
        outline = outline_from_coverage(coverage)
        self._outline_source_points = len(coverage)
        if not outline:
            return data
        current = data.map.info.outline
        if current and polygon_area(outline) < polygon_area(current) * 0.9:
            # The app keeps the lawn contour stable across jobs. A new job
            # resets the track, so its partial coverage would trace a much
            # smaller shape — keep the persisted full-lawn outline instead.
            return data
        return replace(
            data,
            map=replace(
                data.map,
                info=replace(
                    data.map.info,
                    outline=outline,
                    outline_source=OUTLINE_SOURCE_COVERAGE,
                ),
            ),
        )

    def _compact_live_position_segment(self, state: MowerState) -> MowerState:
        """Keep the live position segment since the last trace commit."""
        current = state.map.current_position
        if (
            state.activity is not MowerActivity.MOWING
            or current is None
            or current.invalid == 1
        ):
            return state

        history = state.map.position_history
        compact_history = history or (current,)
        if compact_history[-1] != current:
            compact_history = (*compact_history, current)
        if len(compact_history) > LIVE_POSITION_SEGMENT_MAX_POINTS:
            compact_history = compact_history[-LIVE_POSITION_SEGMENT_MAX_POINTS:]
        if compact_history == history:
            return state
        return replace(
            state,
            map=replace(state.map, position_history=compact_history),
        )

    def _reset_live_position_segment(self, state: MowerState) -> MowerState:
        """Start a new live segment after the mower trace has caught up."""
        current = state.map.current_position
        history = (current,) if current is not None and current.invalid != 1 else ()
        if state.map.position_history == history:
            return state
        return replace(
            state,
            map=replace(state.map, position_history=history),
        )

    def _schedule_trace_refresh(self) -> None:
        """Request the mower trace after enough live heading change."""
        if self._trace_refresh_task and not self._trace_refresh_task.done():
            return
        self._trace_refresh_task = self._create_background_task(
            self._async_refresh_trace_after_turn(),
            "ecovacs_goat_trace_after_turn",
        )

    async def _async_refresh_trace_after_turn(self) -> None:
        """Refresh mower-provided trace after the direction gate opens."""
        await asyncio.sleep(0.5)
        if not self.data or not self.data.map.mid or not self._trace_update_due:
            return
        if not self._protocol.map_api_uses_v2:
            return
        try:
            response = await self.api.control(
                self.device,
                "getMapTrace_V2",
                {"mid": self.data.map.mid, "type": MAP_TRACE_TYPE},
            )
            previous_state = self.data
            state = apply_response(previous_state, "getMapTrace_V2", response)
            trace_changed = self._trace_path_changed(previous_state, state)
            if trace_changed:
                state = self._reset_live_position_segment(state)
                self._mark_trace_mqtt_applied()
            self._publish_refreshed(previous_state, state)
            self._capture_event(
                "trace_refresh_after_turn",
                {
                    "trace_changed": trace_changed,
                    "threshold_degrees": MAP_TRACE_DIRECTION_THRESHOLD_DEGREES,
                    "map_revision": state.map.revision,
                },
            )
        except EcovacsApiError as err:
            _LOGGER.debug("ECOVACS trace refresh after turn failed: %s", err)
            self._capture_event(
                "trace_refresh_after_turn_error",
                {"exception": repr(err)},
            )
        finally:
            if self._trace_refresh_task is asyncio.current_task():
                self._trace_refresh_task = None

    @staticmethod
    def _angle_delta(previous: float, current: float) -> float:
        """Return the shortest signed angular delta between two headings."""
        return (current - previous + 180) % 360 - 180

    def _schedule_mqtt_readback(self) -> None:
        """Debounce a full readback after MQTT reports an actionable change."""
        if self._mqtt_readback_task and not self._mqtt_readback_task.done():
            self._mqtt_readback_task.cancel()
        self._mqtt_readback_task = self._create_background_task(
            self._async_debounced_mqtt_readback(),
            "ecovacs_goat_mqtt_readback",
        )

    def _publish_refreshed(self, base: MowerState | None, refreshed: MowerState) -> MowerState:
        """Publish a refresher's result without reverting fresher pushes.

        ``base`` is the snapshot the refresher started from. Every field a
        push moved while the refresher was awaiting its HTTP calls keeps the
        pushed value; the refresh still contributes the fields only it polls.
        See state_merge.merge_refreshed_state for the full story.
        """
        merged = merge_refreshed_state(base, refreshed, self.data)
        if self._debug_capture is not None and merged is not refreshed:
            kept = changed_field_names(merged, refreshed)
            if kept:
                self._capture_event(
                    "refresh_merge_kept_current", {"fields": list(kept)}
                )
        self.async_set_updated_data(merged)
        return merged

    async def _async_refresh_groups_and_publish(self) -> MowerState:
        """Run a grouped refresh and publish it through the merge."""
        base = self.data
        refreshed = await self._async_refresh_state_groups()
        return self._publish_refreshed(base, refreshed)

    async def _async_debounced_mqtt_readback(self) -> None:
        """Refresh grouped data after related MQTT pushes have settled."""
        try:
            await asyncio.sleep(MQTT_READBACK_DEBOUNCE_SECONDS)
            await self._async_refresh_groups_and_publish()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("ECOVACS actionable MQTT readback failed", exc_info=True)
            self._capture_event(
                "mqtt_readback_error",
                {"exception": repr(err)},
            )

    async def _async_availability_watchdog(self) -> None:
        """Mark entities unavailable when the mower is silent AND unreachable.

        Without this nothing ever sets ``available=False``: after a cloud or
        network outage HA would keep presenting the last state (even
        "mowing") as current indefinitely.
        """
        while not self._stopped:
            await asyncio.sleep(AVAILABILITY_CHECK_SECONDS)
            if self._stopped:
                return
            freshest = max(
                (
                    stamp
                    for stamp in (self._last_mqtt_at, self._last_readback_at)
                    if stamp is not None
                ),
                default=None,
            )
            if (
                freshest is not None
                and monotonic() - freshest < AVAILABILITY_STALE_SECONDS
            ):
                continue
            base = self.data
            try:
                state = await self._async_refresh_state_groups()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - go unavailable instead
                if self.data is not None and self.data.available:
                    _LOGGER.warning(
                        "ECOVACS mower unreachable (%s); marking unavailable", err
                    )
                    self.async_set_updated_data(
                        replace(self.data, available=False)
                    )
                continue
            self._publish_refreshed(base, state)

    def _ensure_returning_refresh(self) -> None:
        """Poll lightly while returning because ECOVACS may stop position pushes at dock."""
        if self._returning_refresh_task and not self._returning_refresh_task.done():
            return
        self._returning_refresh_task = self._create_background_task(
            self._async_refresh_while_returning(),
            "ecovacs_goat_returning_refresh",
        )

    async def _async_refresh_while_returning(self) -> None:
        """Refresh until the mower reports a final docked/idle state.

        A transient cloud/network error must not kill the loop: ECOVACS may
        stop position pushes at dock, so this poll can be the only way HA
        notices the mower has finished returning.
        """
        while self.data and self.data.activity is MowerActivity.RETURNING:
            await asyncio.sleep(RETURNING_REFRESH_SECONDS)
            if not self.data or self.data.activity is not MowerActivity.RETURNING:
                return
            try:
                await self._async_refresh_groups_and_publish()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep the loop alive
                _LOGGER.debug("ECOVACS returning refresh failed: %s", err)
                self._capture_event(
                    "returning_refresh_error",
                    {"exception": repr(err)},
                )

    def _maybe_auto_extend_live_map(self) -> None:
        """Keep the app-style live map session alive while mowing.

        Gated by the ``auto_live_map`` config-entry option. Extends a rolling
        keepalive window; the keepalive task itself stops pinging once the
        mower leaves the live-stream activities, and the window expires within
        ``AUTO_LIVE_MAP_KEEPALIVE_SECONDS`` of the last extension.
        """
        if self._auto_live_map_fn is None or not self._auto_live_map_fn():
            return
        self._extend_live_position_keepalive(
            "auto_live_map",
            duration_seconds=AUTO_LIVE_MAP_KEEPALIVE_SECONDS,
            force=False,
        )

    def _ensure_mowing_position_refresh(self) -> None:
        """Start the conservative live-position fallback while mowing.

        MQTT onPos is the preferred position source. This task only fills gaps when
        onPos has gone stale; it is not intended to drive normal map animation.
        """
        self._maybe_auto_extend_live_map()
        if (
            self._mowing_position_refresh_task
            and not self._mowing_position_refresh_task.done()
        ):
            return
        self._mowing_position_refresh_task = self._create_background_task(
            self._async_refresh_position_while_mowing(),
            "ecovacs_goat_mowing_position_refresh",
        )

    async def _async_refresh_position_while_mowing(self) -> None:
        """Refresh live position only when mowing and MQTT position is stale.

        Without MQTT onPos, heading-based trace gating never opens; still fetch the
        mower trace on the same slow poll so completed mowing lines stay visible.
        """
        while self.data and self.data.activity is MowerActivity.MOWING:
            await asyncio.sleep(MOWING_POSITION_REFRESH_SECONDS)
            if not self.data or self.data.activity is not MowerActivity.MOWING:
                return
            self._maybe_auto_extend_live_map()
            if self._has_recent_position_mqtt():
                continue
            try:
                base = self.data
                state = await self._async_refresh_live_position(base)
                self._publish_refreshed(
                    base, self._compact_live_position_segment(state)
                )
                self._trace_update_due = True
                self._schedule_trace_refresh()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep the loop alive
                _LOGGER.debug("ECOVACS mowing position refresh failed: %s", err)
                self._capture_event(
                    "mowing_position_refresh_error",
                    {"exception": repr(err)},
                )

    def _has_recent_position_mqtt(self) -> bool:
        """Return whether MQTT has recently provided live mower position."""
        return (
            self._last_position_mqtt_at is not None
            and monotonic() - self._last_position_mqtt_at <= POSITION_MQTT_STALE_SECONDS
        )

    async def async_request_live_position_stream(
        self,
        reason: str,
        *,
        force: bool = False,
        duration_seconds: int | None = None,
    ) -> None:
        """Request fast app-style position updates for a visible live map card."""
        if duration_seconds is not None and duration_seconds > 0:
            self._extend_live_position_keepalive(
                reason,
                duration_seconds=duration_seconds,
                force=force,
            )
            return

        base = self.data or self._base_state()
        state = await self._async_request_live_position_stream(
            base,
            reason,
            force=force,
        )
        self._publish_refreshed(base, self._compact_live_position_segment(state))

    def _extend_live_position_keepalive(
        self,
        reason: str,
        *,
        duration_seconds: int,
        force: bool,
    ) -> None:
        """Keep the app-style map session alive for an explicit short window."""
        duration_seconds = max(1, duration_seconds)
        until = monotonic() + duration_seconds
        self._live_position_keepalive_until = max(
            self._live_position_keepalive_until or 0,
            until,
        )
        self._capture_event(
            "live_position_keepalive_extended",
            {
                "reason": reason,
                "force": force,
                "duration_seconds": duration_seconds,
                "until_in_seconds": round(
                    self._live_position_keepalive_until - monotonic(), 1
                ),
            },
        )
        # A later caller may upgrade the running window (e.g. a forced manual
        # request while the auto keepalive runs) — the task reads these live
        # instead of freezing its first-call arguments.
        self._live_position_keepalive_reason = reason
        if force:
            self._live_position_keepalive_force = True
        if (
            self._live_position_keepalive_task is None
            or self._live_position_keepalive_task.done()
        ):
            self._live_position_keepalive_force = force
            self._live_position_keepalive_task = self._create_background_task(
                self._async_live_position_keepalive(),
                "ecovacs_goat_live_position_keepalive",
            )

    async def _async_live_position_keepalive(self) -> None:
        """Send app-style ping/map requests during an explicit keepalive window."""
        try:
            while (
                self._live_position_keepalive_until is not None
                and monotonic() < self._live_position_keepalive_until
            ):
                reason = self._live_position_keepalive_reason
                force = self._live_position_keepalive_force
                base = self.data or self._base_state()
                state = base
                if force or state.activity in LIVE_POSITION_STREAM_ACTIVITIES:
                    try:
                        await self._async_send_app_ping(reason)
                        state = await self._async_request_live_position_stream(
                            state,
                            f"{reason}_keepalive",
                            force=force,
                        )
                        self._publish_refreshed(
                            base, self._compact_live_position_segment(state)
                        )
                        # Safety net for the plan broadcast: a running job
                        # whose map still has no plan (a lone lane, no border
                        # announcement) means the connect edge was missed —
                        # produce another one. Rate-limited to one cycle per
                        # two minutes, so the tail of a job (where the plan
                        # legitimately dwindles) costs a no-op at worst.
                        if (
                            state.activity is MowerActivity.MOWING
                            and len(state.map.trace.lanes) <= 1
                            and state.map.trace.border_template is None
                        ):
                            self._schedule_app_presence_cycle("plan_missing")
                    except EcovacsApiError as err:
                        _LOGGER.debug("ECOVACS live position keepalive failed: %s", err)
                        self._capture_event(
                            "live_position_keepalive_error",
                            {"reason": reason, "exception": repr(err)},
                        )

                if self._live_position_keepalive_until is None:
                    return
                delay = min(
                    APP_PING_INTERVAL_SECONDS,
                    max(0, self._live_position_keepalive_until - monotonic()),
                )
                if delay <= 0:
                    return
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        finally:
            if self._live_position_keepalive_task is asyncio.current_task():
                self._live_position_keepalive_task = None
                self._live_position_keepalive_until = None

    async def _async_send_app_ping(self, reason: str) -> None:
        """Send the GOAT app's lightweight MQTT keepalive command."""
        await self.api.control(self.device, "appping", {})
        self._capture_event("app_ping_sent", {"reason": reason})

    def _schedule_live_position_stream_request(
        self, reason: str, *, force: bool = False
    ) -> None:
        """Request the app-style live map stream in the background."""
        if self._live_position_stream_task and not self._live_position_stream_task.done():
            return
        self._live_position_stream_task = self._create_background_task(
            self._async_request_live_position_stream_background(reason, force=force),
            "ecovacs_goat_live_position_stream",
        )

    async def _async_request_live_position_stream_background(
        self, reason: str, *, force: bool
    ) -> None:
        """Run the app-style live map stream request and merge any readbacks."""
        try:
            base = self.data or self._base_state()
            state = await self._async_request_live_position_stream(
                base,
                reason,
                force=force,
            )
            self._publish_refreshed(base, self._compact_live_position_segment(state))
        except asyncio.CancelledError:
            raise
        except EcovacsApiError as err:
            _LOGGER.debug("ECOVACS live position stream request failed: %s", err)
            self._capture_event(
                "live_position_stream_request_error",
                {"reason": reason, "exception": repr(err)},
            )
        finally:
            if self._live_position_stream_task is asyncio.current_task():
                self._live_position_stream_task = None

    async def _async_request_live_position_stream(
        self,
        state: MowerState,
        reason: str,
        *,
        force: bool = False,
    ) -> MowerState:
        """Ask ECOVACS for the app map view, which triggers fast onPos pushes."""
        if not force and state.activity not in LIVE_POSITION_STREAM_ACTIVITIES:
            self._capture_event(
                "live_position_stream_request_skipped",
                {
                    "reason": reason,
                    "cause": "not_in_live_stream_activity",
                    "activity": state.activity.value
                    if state.activity is not None
                    else None,
                },
            )
            return state

        await self._async_keep_app_presence_mqtt(reason)

        now = monotonic()
        if (
            not force
            and self._last_live_position_stream_request_at is not None
            and now - self._last_live_position_stream_request_at
            < LIVE_POSITION_STREAM_REQUEST_MIN_INTERVAL_SECONDS
        ):
            self._capture_event(
                "live_position_stream_request_skipped",
                {
                    "reason": reason,
                    "cause": "recently_requested",
                    "min_interval_seconds": LIVE_POSITION_STREAM_REQUEST_MIN_INTERVAL_SECONDS,
                },
            )
            return state

        self._last_live_position_stream_request_at = now
        state = await self._async_refresh_live_position(state)

        if self._capability.map_dialect is MapDialect.MAP_RTK:
            # O-series map layers are requested with their own dialect; the
            # geometry then arrives on the onMI / onArI / onMapTrack pushes
            # this request opens.
            state = await self._async_refresh_rtk_map(state)
            self._capture_event(
                "live_position_stream_requested",
                {"reason": reason, "force": force, "mid": state.map.mid, "dialect": "rtk"},
            )
            return state

        mid = state.map.mid
        if not mid:
            self._capture_event(
                "live_position_stream_request_skipped",
                {"reason": reason, "cause": "missing_mid"},
            )
            return state

        trace_changed = False
        if self._protocol.map_api_uses_v2:
            try:
                await self.api.control(
                    self.device,
                    "getMapSet_V2",
                    self._app_live_map_payload(mid, "ar"),
                )
                previous_state = state
                response = await self.api.control(
                    self.device,
                    "getMapTrace_V2",
                    self._app_live_map_payload(mid, MAP_TRACE_TYPE),
                )
                state = apply_response(state, "getMapTrace_V2", response)
                trace_changed = self._trace_path_changed(previous_state, state)
                if trace_changed:
                    state = self._reset_live_position_segment(state)
                    self._mark_trace_mqtt_applied()

                for map_type in APP_LIVE_MAP_TYPES[1:]:
                    await self.api.control(
                        self.device,
                        "getMapSet_V2",
                        self._app_live_map_payload(mid, map_type),
                    )
                await self.api.control(
                    self.device,
                    "getMapPoint",
                    {"mid": mid, "bdTaskID": self._next_app_bd_task_id()},
                )
            except EcovacsApiError as err:
                _LOGGER.warning(
                    "ECOVACS live map stream V2 calls failed; using position/MQTT only: %s",
                    err,
                )
                self._protocol = replace(self._protocol, map_api_uses_v2=False)

        self._capture_event(
            "live_position_stream_requested",
            {
                "reason": reason,
                "force": force,
                "mid": mid,
                "map_types": APP_LIVE_MAP_TYPES,
                "trace_changed": trace_changed,
                "map_api_uses_v2": self._protocol.map_api_uses_v2,
            },
        )
        return state

    def _schedule_app_presence_cycle(self, reason: str) -> None:
        """Restart the app-presence session to produce a fresh connect edge."""
        now = monotonic()
        if (
            self._app_presence_cycled_at is not None
            and now - self._app_presence_cycled_at < 120
        ):
            return
        self._app_presence_cycled_at = now
        self._create_background_task(
            self._async_cycle_app_presence_mqtt(reason),
            "ecovacs_goat_app_presence_cycle",
        )

    async def _async_cycle_app_presence_mqtt(self, reason: str) -> None:
        """Stop and restart the app-presence MQTT session.

        The official app produces this edge naturally by being opened; the
        mower answers it by broadcasting the full plan of the running job.
        """
        try:
            await self._app_presence_mqtt.stop()
        except Exception as err:  # noqa: BLE001 - experimental side channel only
            _LOGGER.debug("ECOVACS app-presence MQTT stop failed: %s", err)
        await asyncio.sleep(1.0)
        await self._async_keep_app_presence_mqtt(reason)
        self._capture_event("app_presence_mqtt_cycled", {"reason": reason})

    async def _async_keep_app_presence_mqtt(self, reason: str) -> None:
        """Keep the captured official-app presence session alive for visible cards."""
        self._app_presence_stop_at = monotonic() + APP_PRESENCE_MQTT_TTL_SECONDS
        try:
            await self._app_presence_mqtt.start()
        except Exception as err:  # noqa: BLE001 - experimental side channel only
            _LOGGER.warning("ECOVACS app-presence MQTT start failed: %s", err)
            self._capture_event(
                "app_presence_mqtt_start_error",
                {"reason": reason, "exception": repr(err)},
            )
            return

        self._capture_event(
            "app_presence_mqtt_keepalive",
            {
                "reason": reason,
                "ttl_seconds": APP_PRESENCE_MQTT_TTL_SECONDS,
            },
        )
        if (
            self._app_presence_stop_task is None
            or self._app_presence_stop_task.done()
        ):
            self._app_presence_stop_task = self._create_background_task(
                self._async_stop_app_presence_mqtt_when_idle(),
                "ecovacs_goat_app_presence_mqtt_stop",
            )

    async def _async_stop_app_presence_mqtt_when_idle(self) -> None:
        """Stop the app-presence MQTT session after card requests stop arriving."""
        try:
            while self._app_presence_stop_at is not None:
                delay = self._app_presence_stop_at - monotonic()
                if delay <= 0:
                    break
                await asyncio.sleep(delay)
            self._app_presence_stop_at = None
            await self._app_presence_mqtt.stop()
        except asyncio.CancelledError:
            raise
        finally:
            if self._app_presence_stop_task is asyncio.current_task():
                self._app_presence_stop_task = None

    def _app_live_map_payload(self, mid: str, map_type: str) -> dict[str, str]:
        """Return the captured app-style map-view command body."""
        return {
            "mid": mid,
            "type": map_type,
            "bdTaskID": self._next_app_bd_task_id(),
        }

    def _next_app_bd_task_id(self) -> str:
        """Return an app-like per-request id used in captured map-view calls."""
        self._live_map_request_counter = (self._live_map_request_counter + 1) % 1000
        return f"{int(time() * 1000)}{self._live_map_request_counter:03d}"

    def _schedule_outcome_poll(
        self,
        key: str,
        predicate: Callable[[MowerState], bool],
        *,
        timeout: int = COMMAND_VERIFY_TIMEOUT_SECONDS,
        interval: int = COMMAND_VERIFY_INTERVAL_SECONDS,
        initial_delay: int = COMMAND_VERIFY_INITIAL_DELAY_SECONDS,
    ) -> None:
        """Verify a command outcome with bounded readback polling."""
        existing = self._outcome_refresh_tasks.get(key)
        if existing and not existing.done():
            existing.cancel()
        self._outcome_refresh_tasks[key] = self._create_background_task(
            self._async_poll_until_outcome(
                key,
                predicate,
                timeout=timeout,
                interval=interval,
                initial_delay=initial_delay,
            ),
            f"ecovacs_goat_outcome_{key}",
        )

    async def _async_poll_until_outcome(
        self,
        key: str,
        predicate: Callable[[MowerState], bool],
        *,
        timeout: int,
        interval: int,
        initial_delay: int,
    ) -> None:
        """Poll grouped state until the expected command result is observed."""
        try:
            await asyncio.sleep(initial_delay)
            deadline = monotonic() + timeout
            while monotonic() <= deadline:
                # MQTT usually confirms the outcome within the initial delay;
                # checking the live state first lets most polls finish without
                # a single HTTP call — after a dock this loop used to hammer
                # grouped refreshes for up to four minutes.
                if self.data is not None and predicate(self.data):
                    self._capture_event(
                        "command_outcome_confirmed",
                        {
                            "key": key,
                            "source": "current",
                            "activity": self.data.activity.value
                            if self.data.activity is not None
                            else None,
                        },
                    )
                    return
                state = await self._async_refresh_groups_and_publish()
                if predicate(state):
                    self._capture_event(
                        "command_outcome_confirmed",
                        {
                            "key": key,
                            "source": "refresh",
                            "activity": state.activity.value
                            if state.activity is not None
                            else None,
                        },
                    )
                    return
                await asyncio.sleep(interval)
            _LOGGER.debug("ECOVACS command outcome %s was not confirmed in time", key)
            self._capture_event("command_outcome_timeout", {"key": key})
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.debug("ECOVACS command outcome poll %s failed", key, exc_info=True)
            self._capture_event(
                "command_outcome_error",
                {"key": key, "exception": repr(err)},
            )
        finally:
            if self._outcome_refresh_tasks.get(key) is asyncio.current_task():
                self._outcome_refresh_tasks.pop(key, None)

    async def async_refresh_if_stale(self) -> None:
        """Refresh grouped state if MQTT/readback data is stale."""
        if self._has_fresh_state():
            return
        _LOGGER.debug(
            "Refreshing ECOVACS mower state before command because live updates are stale"
        )
        await self._async_refresh_groups_and_publish()

    async def async_refresh_state(self) -> None:
        """Force a full refresh: state groups, consumables, and totals."""
        base = self.data
        state = await self._async_refresh_state_groups()
        state = await self._async_refresh_extras(state)
        self._publish_refreshed(base, state)

    def _startup_getinfo_groups(self) -> tuple[tuple[str, ...], ...]:
        """Return startup getInfo groups adapted to this model's dialect.

        O-series mowers read mowing status via ``getCleanInfo`` rather than
        ``getCleanInfo_V2``; substitute it up-front so we do not waste retries
        rediscovering that on every device.
        """
        clean_info = self._capability.clean_info_command
        if clean_info == "getCleanInfo_V2":
            return STARTUP_GET_INFO_GROUPS
        return tuple(
            tuple(clean_info if cmd == "getCleanInfo_V2" else cmd for cmd in group)
            for group in STARTUP_GET_INFO_GROUPS
        )

    async def _async_refresh_state_groups(self) -> MowerState:
        """Refresh only the app-captured grouped state/settings payloads."""
        state = self.data or self._base_state()
        for group in self._startup_getinfo_groups():
            state, self._protocol = await apply_resilient_getinfo_group(
                self.api,
                self.device,
                state,
                group,
                self._protocol,
            )
        state = await self._async_refresh_live_map(state)
        self._last_readback_at = monotonic()
        return state

    async def _async_refresh_live_map(self, state: MowerState) -> MowerState:
        """Refresh live map position and request a map trace push."""
        try:
            state = await self._async_refresh_live_position(state)
        except EcovacsApiError as err:
            _LOGGER.debug("ECOVACS live position refresh failed: %s", err)
            return state

        if self._capability.map_dialect is MapDialect.MAP_RTK:
            return await self._async_refresh_rtk_map(state)

        if not self._protocol.map_api_uses_v2:
            return state
        try:
            if state.map.mid:
                if not state.map.info.outline:
                    map_info_payload: dict[str, Any] = {
                        "mid": state.map.mid,
                        "using": 0,
                        "serial": 0,
                        "index": 0,
                        "type": MAP_TRACE_TYPE,
                    }
                    if state.task_id:
                        map_info_payload["bdTaskID"] = state.task_id
                    response = await self.api.control(
                        self.device,
                        "getMapInfo_V2",
                        map_info_payload,
                    )
                    state = apply_response(state, "getMapInfo_V2", response)
                response = await self.api.control(
                    self.device,
                    "getMapTrace_V2",
                    {"mid": state.map.mid, "type": MAP_TRACE_TYPE},
                )
                state = apply_response(state, "getMapTrace_V2", response)
        except EcovacsApiError as err:
            if self._protocol.map_api_uses_v2:
                _LOGGER.warning(
                    "ECOVACS map V2 control API unavailable for this device; "
                    "continuing with MQTT/position-only map updates: %s",
                    err,
                )
                self._protocol = replace(self._protocol, map_api_uses_v2=False)
            else:
                _LOGGER.debug("ECOVACS live map refresh failed: %s", err)
        return state

    async def _async_refresh_rtk_map(self, state: MowerState) -> MowerState:
        """Best-effort O-series (RTK) map refresh.

        The shared position stream refreshed above provides the live marker (and
        the map id via ``getPos``). O-series mowers use the ``getMapState`` /
        ``getMapTrack`` / ``getAreaSet`` dialect rather than the G1 ``*_V2``
        calls. We read:

        * ``getMapState`` for build status,
        * ``getRTK`` for the fixed base station position,
        * ``getMapTrack`` for virtual walls and ``getAreaSet`` for mowing areas
          (their ``subsets`` blobs decode with the shared LZMA decoder).

        The base-map outline itself is pushed over MQTT, not returned here, so it
        is not part of this poll. The orchestration lives in
        :func:`.mower_compat.refresh_rtk_map` so it can be unit-tested without a
        Home Assistant runtime.
        """
        return await refresh_rtk_map(
            self.api, self.device, state, capture=self._capture_event
        )

    async def _async_refresh_live_position(self, state: MowerState) -> MowerState:
        """Refresh the mower, charger, and beacon positions."""
        new_state, self._protocol = await refresh_live_position(
            self.api, self.device, state, self._protocol
        )
        return new_state

    async def _async_refresh_live_map_after_mqtt_start(self) -> None:
        """Request live map data after MQTT has had time to subscribe."""
        for delay in (5, 10):
            await asyncio.sleep(delay)
            base = self.data or self._base_state()
            self._publish_refreshed(
                base, await self._async_refresh_live_map(base)
            )
            if self.data and self.data.map.trace.path:
                return

    def _has_fresh_state(self) -> bool:
        """Return whether MQTT or a recent readback has fresh state."""
        last_seen = max(
            (stamp for stamp in (self._last_mqtt_at, self._last_readback_at) if stamp),
            default=None,
        )
        return last_seen is not None and monotonic() - last_seen <= FRESH_STATE_SECONDS

    def _capture_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Write a coordinator capture event if capture is active."""
        if self._debug_capture is None:
            return
        self._debug_capture.capture_event(
            event_type,
            {
                "device": {
                    "did": self.device.did,
                    "class": self.device.device_class,
                    "resource": self.device.resource,
                    "model": self.device.model,
                },
                **data,
            },
        )

    def _create_background_task(
        self, coro: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        """Create a non-startup-blocking task where supported by Home Assistant."""
        if hasattr(self.hass, "async_create_background_task"):
            return self.hass.async_create_background_task(coro, name)
        return asyncio.create_task(coro, name=name)

    def _base_state(self) -> MowerState:
        """Return an empty cache row with static per-device fields filled in."""
        return replace(
            MowerState(),
            goat_variant=classify_goat_variant(self.device.model),
            mower_family=str(self._capability.family),
        )

    @property
    def protocol_profile(self) -> dict[str, Any]:
        """Return learned protocol details for diagnostics."""
        return {
            **self._protocol.as_dict(),
            "goat_variant": self.data.goat_variant
            if self.data
            else classify_goat_variant(self.device.model),
            "capability_profile": self._capability.as_dict(),
            "device_name": self.device.model,
        }

    async def control(
        self,
        command: str,
        data: Any | None = None,
        *,
        refresh_if_stale: bool = True,
    ) -> None:
        """Execute a command and merge the response into the cache."""
        if refresh_if_stale:
            await self.async_refresh_if_stale()
        payload = self._command_payload(command, data or {})
        response = await self.api.control(self.device, command, payload)
        self.async_set_updated_data(apply_response(self.data, command, response))

    async def start_mowing(self) -> None:
        """Start or resume mowing using app-captured clean_V2 bodies."""
        await self.async_refresh_if_stale()
        previous_activity = self.data.activity
        act = (
            "resume"
            if previous_activity in {MowerActivity.PAUSED, MowerActivity.RETURNING}
            else "start"
        )
        await self.control(
            self._capability.clean_command,
            # Resuming must name the job that is actually open (a paused edge
            # trim resumes as "borderrotate", not "auto") — the mower ignores
            # a clean act whose content.type does not match the open job.
            self._capability.clean_body(
                act, self.data.clean_type if act == "resume" else None
            ),
            refresh_if_stale=False,
        )
        mower_map = self.data.map
        if previous_activity not in {
            MowerActivity.MOWING,
            MowerActivity.PAUSED,
            MowerActivity.RETURNING,
        }:
            mower_map = replace(mower_map, position_history=())
        self.async_set_updated_data(
            replace(
                self.data,
                activity=MowerActivity.MOWING,
                map=mower_map,
            )
        )
        self._ensure_mowing_position_refresh()
        self._schedule_outcome_poll(
            "start_mowing", lambda state: state.activity is MowerActivity.MOWING
        )

    async def start_edge_trim(self) -> None:
        """Start an edge-trimming job (the app's border-cut mode).

        Edge trimming is a standalone job type — ``clean`` with
        ``content.type = "borderrotate"`` — not a phase of a normal mow, so it
        can only start while the mower is idle or docked.
        """
        await self.async_refresh_if_stale()
        previous_activity = self.data.activity
        if previous_activity in {
            MowerActivity.MOWING,
            MowerActivity.PAUSED,
            MowerActivity.RETURNING,
        }:
            raise HomeAssistantError(
                "Edge trimming is a separate job; end the current job first."
            )
        await self.control(
            self._capability.clean_command,
            {
                "act": "start",
                "content": {
                    "type": EDGE_TRIM_CONTENT_TYPE,
                    "value": EDGE_TRIM_CONTENT_VALUE,
                },
            },
            refresh_if_stale=False,
        )
        self.async_set_updated_data(
            replace(
                self.data,
                activity=MowerActivity.MOWING,
                clean_type=EDGE_TRIM_CONTENT_TYPE,
                map=replace(self.data.map, position_history=()),
            )
        )
        self._ensure_mowing_position_refresh()
        self._schedule_outcome_poll(
            "start_edge_trim", lambda state: state.activity is MowerActivity.MOWING
        )

    async def pause(self) -> None:
        """Pause active mowing."""
        await self.async_refresh_if_stale()
        await self.control(
            self._capability.clean_command,
            self._capability.clean_body("pause", self.data.clean_type),
            refresh_if_stale=False,
        )
        self.async_set_updated_data(replace(self.data, activity=MowerActivity.PAUSED))
        self._schedule_outcome_poll(
            "pause", lambda state: state.activity is MowerActivity.PAUSED
        )

    async def end_mowing(self) -> None:
        """End the active mowing session."""
        await self.async_refresh_if_stale()
        await self.control(
            self._capability.clean_command,
            # Stop must name the open job: a stop typed "auto" during an edge
            # trim is acked and ignored (the trim could not be ended from HA).
            self._capability.clean_body("stop", self.data.clean_type),
            refresh_if_stale=False,
        )
        # Stop ends the job for good, so clear its type in the optimistic
        # publish too: leaving the stale "borderrotate"/"auto" for the seconds
        # until the mower's own push confirmed it made the tile flash through
        # its default branch ("ready — at the dock" while standing mid-lawn).
        self.async_set_updated_data(
            replace(self.data, activity=MowerActivity.IDLE, clean_type=None)
        )
        self._schedule_outcome_poll(
            "end_mowing",
            lambda state: state.activity in {MowerActivity.IDLE, MowerActivity.DOCKED},
        )

    async def dock(self) -> None:
        """Return to charge, or cancel an active return-to-charge command."""
        await self.async_refresh_if_stale()
        if self.data.activity is MowerActivity.RETURNING:
            await self.control("charge", {"act": "stop"}, refresh_if_stale=False)
            self.async_set_updated_data(replace(self.data, activity=MowerActivity.PAUSED))
            self._schedule_outcome_poll(
                "dock",
                lambda state: state.activity
                in {MowerActivity.PAUSED, MowerActivity.MOWING, MowerActivity.IDLE},
            )
            return

        await self.control("charge", {"act": "go"}, refresh_if_stale=False)
        self.async_set_updated_data(replace(self.data, activity=MowerActivity.RETURNING))
        self._schedule_outcome_poll(
            "dock",
            lambda state: state.activity is MowerActivity.DOCKED,
            timeout=240,
            interval=RETURNING_REFRESH_SECONDS,
        )
        self._ensure_returning_refresh()

    def _command_payload(self, command: str, data: Any) -> Any:
        """Add the current app task id to write payloads when known."""
        if (
            command not in COMMANDS_WITH_TASK_ID
            or not isinstance(data, dict)
            or not self.data.task_id
            or "bdTaskID" in data
        ):
            return data
        return {**data, "bdTaskID": self.data.task_id}

    async def set_enabled(self, key: str, enabled: bool) -> None:
        """Set a boolean mower setting."""
        await self.async_refresh_if_stale()
        settings = self.data.settings
        match key:
            case "rain_sensor":
                await self.control(
                    "setRainDelay",
                    {"enable": 1 if enabled else 0, "delay": settings.rain_delay or 180},
                    refresh_if_stale=False,
                )
                state = apply_command_data(
                    self.data,
                    "getRainDelay",
                    {"enable": 1 if enabled else 0, "delay": settings.rain_delay or 180},
                )
                predicate = lambda state: state.settings.rain_enabled is enabled
            case "animal_protection":
                await self.control(
                    "setAnimProtect",
                    {
                        "enable": 1 if enabled else 0,
                        "start": settings.animal_start or "19:00",
                        "end": settings.animal_end or "08:00",
                    },
                    refresh_if_stale=False,
                )
                state = apply_command_data(
                    self.data,
                    "getAnimProtect",
                    {
                        "enable": 1 if enabled else 0,
                        "start": settings.animal_start or "19:00",
                        "end": settings.animal_end or "08:00",
                    },
                )
                predicate = lambda state: state.settings.animal_enabled is enabled
            case "ai_recognition":
                await self.control(
                    "setRecognization",
                    {"state": 1 if enabled else 0},
                    refresh_if_stale=False,
                )
                state = apply_command_data(
                    self.data, "getRecognization", {"state": 1 if enabled else 0}
                )
                predicate = lambda state: state.settings.ai_recognition is enabled
            case "border_switch":
                await self.control(
                    "setBorderSwitch",
                    {"enable": 1 if enabled else 0},
                    refresh_if_stale=False,
                )
                state = apply_command_data(
                    self.data,
                    "getBorderSwitch",
                    {"enable": 1 if enabled else 0, "mode": settings.border_mode or 0},
                )
                predicate = lambda state: state.settings.border_switch is enabled
            case "move_up_warning":
                await self.control(
                    "setMoveupWarning",
                    {"enable": 1 if enabled else 0},
                    refresh_if_stale=False,
                )
                state = apply_command_data(
                    self.data, "getMoveupWarning", {"enable": 1 if enabled else 0}
                )
                predicate = lambda state: state.settings.move_up_warning is enabled
            case "cross_map_border_warning":
                await self.control(
                    "setCrossMapBorderWarning",
                    {"enable": 1 if enabled else 0},
                    refresh_if_stale=False,
                )
                state = apply_command_data(
                    self.data,
                    "getCrossMapBorderWarning",
                    {"enable": 1 if enabled else 0},
                )
                predicate = (
                    lambda state: state.settings.cross_map_border_warning is enabled
                )
            case "auto_cut_direction":
                await self.control(
                    "setAutoCutDirection",
                    {"enable": 1 if enabled else 0},
                    refresh_if_stale=False,
                )
                state = apply_command_data(
                    self.data,
                    "getAutoCutDirection",
                    {"enable": 1 if enabled else 0},
                )
                predicate = lambda state: state.settings.auto_cut_direction is enabled
            case _:
                raise ValueError(f"Unsupported switch key {key}")
        self.async_set_updated_data(state)
        self._schedule_outcome_poll(f"set_{key}", predicate)

    async def set_rain_delay(self, delay: int) -> None:
        """Set rain delay in minutes."""
        await self.async_refresh_if_stale()
        enabled = self.data.settings.rain_enabled
        await self.control(
            "setRainDelay",
            {"enable": 1 if enabled else 0, "delay": delay},
            refresh_if_stale=False,
        )
        self.async_set_updated_data(
            apply_command_data(
                self.data, "getRainDelay", {"enable": 1 if enabled else 0, "delay": delay}
            )
        )
        self._schedule_outcome_poll(
            "set_rain_delay", lambda state: state.settings.rain_delay == delay
        )

    async def set_cut_direction(self, angle: int) -> None:
        """Set mowing cut direction.

        The mower silently ignores ``setCutDirection`` while a job is running,
        so refuse the call up-front from any source (UI slider, custom card,
        services, scripts) when the activity indicates active work.
        """
        activity = self.data.activity if self.data else None
        if activity in CUT_DIRECTION_LOCKED_ACTIVITIES:
            raise HomeAssistantError(
                "Cut direction can only be changed while the mower is idle or "
                "docked. End or pause-and-end the current job first."
            )
        await self.async_refresh_if_stale()
        await self.control(
            "setCutDirection", {"angle": angle}, refresh_if_stale=False
        )
        self.async_set_updated_data(
            apply_command_data(self.data, "getCutDirection", {"angle": angle})
        )
        self._schedule_outcome_poll(
            "set_cut_direction", lambda state: state.settings.cut_direction == angle
        )

    async def set_area_mow_height(self, area_id: int, level: int) -> None:
        """Set the cutting-height level of one ``AreaParameters`` record.

        O-series mowers (O1200 LiDAR Pro) manage cutting height through
        ``AreaParameters``; the captured readback (``onAreaParameter``) carries
        the full record set, so the write sends every record back with only the
        requested level changed. The mower broadcasts ``onAreaParameter`` on
        success, which confirms the write.
        """
        await self.async_refresh_if_stale()
        parameters = self.data.settings.area_parameters
        if not any(parameter.area_id == area_id for parameter in parameters):
            raise HomeAssistantError(
                f"Mowing parameters for zone {area_id} are not known yet; "
                "refresh state (or open the ECOVACS app once) so the mower "
                "reports its AreaParameters first."
            )
        updated = tuple(
            replace(parameter, mow_height_level=level)
            if parameter.area_id == area_id
            else parameter
            for parameter in parameters
        )
        await self._async_set_area_parameters(updated)
        self.async_set_updated_data(
            apply_command_data(
                self.data,
                "onAreaParameter",
                {
                    "areaParameters": [
                        parameter.as_payload() for parameter in updated
                    ]
                },
            )
        )
        self._schedule_outcome_poll(
            "set_area_mow_height",
            lambda state: any(
                parameter.area_id == area_id
                and parameter.mow_height_level == level
                for parameter in state.settings.area_parameters
            ),
        )

    async def _async_set_area_parameters(
        self, parameters: tuple[AreaParameter, ...]
    ) -> None:
        """Write AreaParameters, probing the accepted payload shape.

        The readback push wraps records in ``areaParameters``, but the mower
        rejects that same shape on write with "areaID is null", so the flat
        single-record body is tried first; accepted shapes are remembered.
        """
        candidates: list[Any] = []
        if self._area_parameter_write_shape != "wrapped":
            candidates.append(("flat", parameters[0].as_payload()))
        candidates.append(
            (
                "wrapped",
                {
                    "areaParameters": [
                        parameter.as_payload() for parameter in parameters
                    ]
                },
            )
        )

        last_error: EcovacsApiError | None = None
        for shape, payload in candidates:
            try:
                await self.control(
                    "setAreaParameter", payload, refresh_if_stale=False
                )
            except EcovacsApiError as err:
                last_error = err
                self._capture_event(
                    "set_area_parameter_shape_rejected",
                    {"shape": shape, "exception": repr(err)},
                )
                continue
            self._area_parameter_write_shape = shape
            self._capture_event(
                "set_area_parameter_shape_accepted", {"shape": shape}
            )
            return
        assert last_error is not None
        raise last_error

    async def set_volume(self, key: str, level: int) -> None:
        """Set one speaker volume level (0..volume_total).

        ``setVolume`` carries every volume at once, so the untouched ones are
        sent back unchanged. When getVolume has not answered yet, fetch it
        first — falling back to defaults here would silently blast the other
        volumes to maximum.
        """
        await self.async_refresh_if_stale()
        settings = self.data.settings
        if None in (settings.volume, settings.fall_volume, settings.search_volume):
            try:
                response = await self.api.control(self.device, "getVolume", {})
                self.async_set_updated_data(
                    apply_response(self.data, "getVolume", response)
                )
                settings = self.data.settings
            except EcovacsApiError as err:
                _LOGGER.debug("ECOVACS getVolume before setVolume failed: %s", err)
        total = settings.volume_total or VOLUME_DEFAULT_TOTAL
        level = max(0, min(total, level))
        payload = {
            "total": total,
            "volume": settings.volume if settings.volume is not None else total,
            "fallVolume": settings.fall_volume
            if settings.fall_volume is not None
            else total,
            "searchVolume": settings.search_volume
            if settings.search_volume is not None
            else total,
        }
        payload[VOLUME_PAYLOAD_KEYS[key]] = level
        await self.control("setVolume", payload, refresh_if_stale=False)
        self.async_set_updated_data(
            apply_command_data(self.data, "getVolume", payload)
        )
        self._schedule_outcome_poll(
            "set_volume",
            lambda state: getattr(state.settings, key) == level,
        )

    async def set_area_obstacle_height(self, level: int) -> None:
        """Set obstacle-avoidance sensitivity across all mowing zones.

        O-series mowers keep this in ``AreaParameters`` rather than the
        standalone ``setObstacleHeight`` command, so mirror the app and write
        the full record set back with the new level.
        """
        await self.async_refresh_if_stale()
        parameters = self.data.settings.area_parameters
        if not parameters:
            await self.control(
                "setObstacleHeight", {"level": level}, refresh_if_stale=False
            )
            self.async_set_updated_data(
                apply_command_data(self.data, "getObstacleHeight", {"level": level})
            )
        else:
            updated = tuple(
                replace(parameter, obstacle_height=level) for parameter in parameters
            )
            # Same write path as cutting height: the mower rejects the wrapped
            # payload shape on write ("areaID is null"), so probe flat first.
            await self._async_set_area_parameters(updated)
            self.async_set_updated_data(
                apply_command_data(
                    self.data,
                    "onAreaParameter",
                    {
                        "areaParameters": [
                            parameter.as_payload() for parameter in updated
                        ]
                    },
                )
            )
        self._schedule_outcome_poll(
            "set_area_obstacle_height",
            lambda state: state.settings.obstacle_avoidance
            == OBSTACLE_AVOIDANCE_BY_LEVEL.get(level),
        )

    async def set_mowing_efficiency(self, option: str) -> None:
        """Set the mowing speed."""
        await self.async_refresh_if_stale()
        level = self._capability.cut_efficiency_levels.get(
            option, MOWING_EFFICIENCY_LEVELS[option]
        )
        await self.control(
            "setCutEfficiency", {"level": level}, refresh_if_stale=False
        )
        self.async_set_updated_data(
            apply_command_data(self.data, "getCutEfficiency", {"level": level})
        )
        self._schedule_outcome_poll(
            "set_mowing_efficiency",
            lambda state: state.settings.mowing_efficiency == option,
        )

    async def set_obstacle_avoidance(self, option: str) -> None:
        """Set obstacle avoidance mode."""
        await self.set_area_obstacle_height(OBSTACLE_AVOIDANCE_LEVELS[option])

    async def set_animal_time(self, key: str, value: str) -> None:
        """Set animal protection time window."""
        await self.async_refresh_if_stale()
        settings = self.data.settings
        start = value if key == "animal_start" else settings.animal_start or "19:00"
        end = value if key == "animal_end" else settings.animal_end or "08:00"
        enabled = settings.animal_enabled
        await self.control(
            "setAnimProtect",
            {"enable": 1 if enabled else 0, "start": start, "end": end},
            refresh_if_stale=False,
        )
        self.async_set_updated_data(
            apply_command_data(
                self.data,
                "getAnimProtect",
                {"enable": 1 if enabled else 0, "start": start, "end": end},
            )
        )
        self._schedule_outcome_poll(
            f"set_{key}",
            lambda state: (
                state.settings.animal_start if key == "animal_start" else state.settings.animal_end
            )
            == value,
        )
