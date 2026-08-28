"""Models for the ECOVACS GOAT mower driver."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

POSITION_HISTORY_ATTRIBUTE_POINTS = 800
POSITION_HISTORY_DENSE_TAIL_POINTS = 600
# O-series mowers stream the full mowed track (onMapTrack) at roughly one
# point per 1-2 m of travel, so a full session over ~250 m2 needs ~4000
# points. Decimating below that tears the drawn lanes apart (the card splits
# the line at large gaps), so the cap matches the accumulation cap.
TRACE_ATTRIBUTE_POINTS = 4000
# The outline/obstacles arrive as chain codes and are simplified on decode
# (collinear runs collapsed), so these caps only guard against pathological
# shapes rather than trimming normal detail.
OUTLINE_ATTRIBUTE_POINTS = 600
OBSTACLE_ATTRIBUTE_POINTS = 80


class MowerActivity(StrEnum):
    """Internal mower activity values."""

    IDLE = "idle"
    MOWING = "mowing"
    PAUSED = "paused"
    RETURNING = "returning"
    DOCKED = "docked"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MowerDevice:
    """Device details required by ECOVACS N-GIoT and MQTT."""

    did: str
    device_class: str
    resource: str
    name: str
    model: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "MowerDevice":
        """Create a mower device from an ECOVACS device-list entry."""
        name = data.get("nick") or data.get("name") or data.get("deviceName") or "Mower"
        return cls(
            did=data["did"],
            device_class=data["class"],
            resource=data["resource"],
            name=name,
            model=data.get("deviceName"),
            raw=dict(data),
        )


@dataclass(frozen=True)
class NetworkInfo:
    """Network diagnostic state."""

    ip: str | None = None
    ssid: str | None = None
    rssi: int | None = None
    mac: str | None = None


# Cutting height maps inversely to ``AreaParameters.mowHeightLevel``:
# mm = 85 - 5 * level, i.e. levels 1..11 are 80..30 mm. Calibrated against the
# app on an O1200 LiDAR Pro (level 1 -> 80 mm, 6 -> 55, 7 -> 50, 11 -> 30);
# the app slider steps by 5 mm.
CUT_HEIGHT_STEP_MM = 5
CUT_HEIGHT_BASE_MM = 85
CUT_HEIGHT_MIN_MM = 30
CUT_HEIGHT_MAX_MM = 80


def cut_height_mm_from_level(level: int) -> int:
    """Convert a mowHeightLevel to millimetres."""
    return CUT_HEIGHT_BASE_MM - CUT_HEIGHT_STEP_MM * level


def cut_height_level_from_mm(millimetres: float) -> int:
    """Convert millimetres to the nearest mowHeightLevel."""
    return round((CUT_HEIGHT_BASE_MM - millimetres) / CUT_HEIGHT_STEP_MM)


@dataclass(frozen=True)
class AreaParameter:
    """Per-zone mowing parameters (O-series ``AreaParameters`` records).

    ``mow_height_level`` is the mower's cutting-height level; convert it with
    :func:`cut_height_mm_from_level`.
    """

    area_id: int
    mow_height_level: int | None = None
    cut_mode: int | None = None
    obstacle_height: int | None = None
    angle: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a serialisable representation."""
        return {
            "area_id": self.area_id,
            "mow_height_level": self.mow_height_level,
            "cut_mode": self.cut_mode,
            "obstacle_height": self.obstacle_height,
            "angle": self.angle,
        }

    def as_payload(self) -> dict[str, Any]:
        """Return the ECOVACS ``AreaParameters`` record shape.

        ``areaID`` is serialised as a string — that is how the mower's own
        ``onAreaParameter`` pushes encode it, and the firmware's strict parser
        reports "areaID is null" when it receives a number instead.
        """
        payload: dict[str, Any] = {"areaID": str(self.area_id)}
        if self.mow_height_level is not None:
            payload["mowHeightLevel"] = self.mow_height_level
        if self.cut_mode is not None:
            payload["cutMode"] = self.cut_mode
        if self.obstacle_height is not None:
            payload["obstacleHeight"] = self.obstacle_height
        if self.angle is not None:
            payload["angle"] = self.angle
        return payload


@dataclass(frozen=True)
class MowerSettings:
    """Mower configuration values."""

    rain_enabled: bool | None = None
    rain_delay: int | None = None
    animal_enabled: bool | None = None
    animal_start: str | None = None
    animal_end: str | None = None
    ai_recognition: bool | None = None
    border_switch: bool | None = None
    border_mode: int | None = None
    move_up_warning: bool | None = None
    cross_map_border_warning: bool | None = None
    cut_direction: int | None = None
    auto_cut_direction: bool | None = None
    # Speaker volumes as reported by getVolume, each out of ``volume_total``:
    # the main prompt volume, the lift/tilt alarm, and the "find mower" beep.
    volume: int | None = None
    fall_volume: int | None = None
    search_volume: int | None = None
    volume_total: int | None = None
    mowing_efficiency: str | None = None
    obstacle_avoidance: str | None = None
    area_parameters: tuple[AreaParameter, ...] = ()


@dataclass(frozen=True)
class MowerLastJob:
    """Summary of the most recently finished job of one kind.

    The mower's own ``getLastTimeStats`` covers only the single latest task
    and carries no timestamp, so the integration tracks job lifecycles itself
    and keeps one record per kind ("auto" mowing / "borderrotate" edge trim).
    """

    kind: str
    started_at: str | None = None
    ended_at: str | None = None
    mowed_area: float | None = None
    duration_minutes: float | None = None
    task_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable payload for persistence."""
        return {
            "kind": self.kind,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "mowed_area": self.mowed_area,
            "duration_minutes": self.duration_minutes,
            "task_id": self.task_id,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> MowerLastJob | None:
        """Rebuild a record from a persisted payload."""
        if not isinstance(payload, dict) or not payload.get("kind"):
            return None

        def _num(value: Any) -> float | None:
            return float(value) if isinstance(value, (int, float)) else None

        return cls(
            kind=str(payload["kind"]),
            started_at=str(payload["started_at"]) if payload.get("started_at") else None,
            ended_at=str(payload["ended_at"]) if payload.get("ended_at") else None,
            mowed_area=_num(payload.get("mowed_area")),
            duration_minutes=_num(payload.get("duration_minutes")),
            task_id=str(payload["task_id"]) if payload.get("task_id") else None,
        )


@dataclass(frozen=True)
class MowerStats:
    """Mower statistics."""

    area: int | None = None
    job_area: int | None = None
    progress: float | None = None
    duration: int | None = None
    total_area: int | None = None
    total_duration: int | None = None
    total_count: int | None = None


@dataclass(frozen=True)
class MapPosition:
    """Position on the mower map coordinate plane."""

    x: int
    y: int
    a: int | None = None
    invalid: int | None = None
    sn: str | None = None
    z: int | None = None
    t: int | None = None

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "MapPosition | None":
        """Create a map position from an ECOVACS payload item."""
        if data.get("x") is None or data.get("y") is None:
            return None
        try:
            return cls(
                x=int(data["x"]),
                y=int(data["y"]),
                a=int(data["a"]) if data.get("a") is not None else None,
                invalid=int(data["invalid"]) if data.get("invalid") is not None else None,
                sn=str(data["sn"]) if data.get("sn") is not None else None,
                z=int(data["z"]) if data.get("z") is not None else None,
                t=int(data["t"]) if data.get("t") is not None else None,
            )
        except (TypeError, ValueError):
            return None

    def as_dict(self) -> dict[str, Any]:
        """Return a compact serialisable representation."""
        return {
            key: value
            for key, value in {
                "x": self.x,
                "y": self.y,
                "a": self.a,
                "invalid": self.invalid,
                "sn": self.sn,
                "z": self.z,
                "t": self.t,
            }.items()
            if value is not None
        }


@dataclass(frozen=True)
class MowerMapTrace:
    """Chunked live map trace payload pushed by the mower."""

    batch_id: str | None = None
    serial: str | None = None
    info_size: int | None = None
    type: str | None = None
    chunks: dict[int, str] = field(default_factory=dict)
    path: tuple[MapPosition, ...] = ()
    # What the mower still has to cut, keyed by the lane id it assigns.
    # Each lane holds one or more separate segments (a lane split by an
    # obstacle has several), and the mower shrinks them as it works — this is
    # the layer the app hatches over the lawn and rubs out piece by piece.
    # They are NOT one path: joining lanes would draw lines across whatever
    # lies between them.
    lanes: dict[str, tuple[tuple[MapPosition, ...], ...]] = field(
        default_factory=dict
    )
    # The edge lap still to drive, sent chain-coded alongside the lanes. Kept
    # apart because it is drawn differently (it runs along the lawn boundary)
    # and because it can shrink to a straight run, so it cannot be told from a
    # lane by shape alone.
    border: tuple[tuple[MapPosition, ...], ...] = ()

    @property
    def pending_segments(self) -> tuple[tuple[MapPosition, ...], ...]:
        """Return every segment still to be cut, across all lanes."""
        return tuple(
            segment for lane in self.lanes.values() for segment in lane
        )

    @property
    def complete(self) -> bool:
        """Return whether the received chunks look contiguous."""
        if self.path or self.lanes:
            return True
        if not self.chunks:
            return False
        indexes = sorted(self.chunks)
        return indexes == list(range(indexes[-1] + 1))

    def as_dict(self) -> dict[str, Any]:
        """Return serialisable trace metadata and chunks."""
        return {
            "batch_id": self.batch_id,
            "serial": self.serial,
            "type": self.type,
            "info_size": self.info_size,
            "complete": self.complete,
            "chunk_count": len(self.chunks),
            "chunk_indexes": sorted(self.chunks),
            "pending": [
                [position.as_dict() for position in segment]
                for segment in self.pending_segments
            ],
            "border": [
                [position.as_dict() for position in segment]
                for segment in self.border
            ],
            "path": [
                position.as_dict()
                for position in _sample_positions(self.path, TRACE_ATTRIBUTE_POINTS)
            ],
        }


@dataclass(frozen=True)
class MowerMapInfo:
    """Chunked base map payload pushed by the mower."""

    batch_id: str | None = None
    serial: str | None = None
    info_size: int | None = None
    type: str | None = None
    chunks: dict[int, str] = field(default_factory=dict)
    outline: tuple[MapPosition, ...] = ()
    obstacles: tuple[tuple[MapPosition, ...], ...] = ()
    # Where the outline came from: "mower" (its own stored map, exact) or
    # "coverage" (traced from where it drove, a fallback approximation).
    outline_source: str | None = None
    # Map units per chain-code cell, derived from the mower's own payload
    # so obstacles decode on the same grid as the outline.
    chain_step: int | None = None

    @property
    def complete(self) -> bool:
        """Return whether the base map has decoded geometry."""
        return bool(self.outline)

    def as_dict(self) -> dict[str, Any]:
        """Return serialisable map geometry."""
        return {
            "batch_id": self.batch_id,
            "serial": self.serial,
            "type": self.type,
            "info_size": self.info_size,
            "complete": self.complete,
            "chunk_count": len(self.chunks),
            "chunk_indexes": sorted(self.chunks),
            "outline_source": self.outline_source,
            "chain_step": self.chain_step,
            "outline": [
                position.as_dict()
                for position in _sample_positions(self.outline, OUTLINE_ATTRIBUTE_POINTS)
            ],
            "obstacles": [
                [
                    position.as_dict()
                    for position in _sample_positions(
                        obstacle, OBSTACLE_ATTRIBUTE_POINTS
                    )
                ]
                for obstacle in self.obstacles
            ],
        }


@dataclass(frozen=True)
class MowerMap:
    """Live map data used by the Lovelace card."""

    mid: str | None = None
    current_position: MapPosition | None = None
    charge_positions: tuple[MapPosition, ...] = ()
    uwb_positions: tuple[MapPosition, ...] = ()
    rtk_station: MapPosition | None = None
    areas: tuple[MapPosition, ...] = ()
    no_go_zones: tuple[tuple[MapPosition, ...], ...] = ()
    position_history: tuple[MapPosition, ...] = ()
    info: MowerMapInfo = field(default_factory=MowerMapInfo)
    trace: MowerMapTrace = field(default_factory=MowerMapTrace)
    last_update_ts: int | None = None
    revision: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Return a serialisable map snapshot."""
        return {
            "mid": self.mid,
            "current_position": self.current_position.as_dict()
            if self.current_position
            else None,
            "charge_positions": [position.as_dict() for position in self.charge_positions],
            "uwb_positions": [position.as_dict() for position in self.uwb_positions],
            "rtk_station": self.rtk_station.as_dict() if self.rtk_station else None,
            "areas": [position.as_dict() for position in self.areas],
            "no_go_zones": [
                [position.as_dict() for position in zone] for zone in self.no_go_zones
            ],
            "position_history": [
                position.as_dict()
                for position in _sample_positions(
                    self.position_history,
                    POSITION_HISTORY_ATTRIBUTE_POINTS,
                    dense_tail=POSITION_HISTORY_DENSE_TAIL_POINTS,
                )
            ],
            "info": self.info.as_dict(),
            "trace": self.trace.as_dict(),
            "last_update_ts": self.last_update_ts,
            "revision": self.revision,
        }


def _sample_positions(
    positions: tuple[MapPosition, ...], max_points: int, *, dense_tail: int = 0
) -> tuple[MapPosition, ...]:
    """Return a bounded path for Home Assistant state attributes.

    Keep the recent tail dense for live movement so the card does not suddenly
    lose the newest path shape when the full history grows beyond the limit.
    """
    if len(positions) <= max_points:
        return positions
    if dense_tail > 0:
        tail_size = min(dense_tail, max_points)
        head_limit = max_points - tail_size
        tail = positions[-tail_size:]
        head = positions[:-tail_size]
        if head_limit <= 0:
            return tail
        sampled_head = _sample_positions(head, head_limit)
        return (*sampled_head, *tail)
    step = max(1, (len(positions) + max_points - 1) // max_points)
    sampled = positions[::step]
    if sampled[-1] != positions[-1]:
        sampled = (*sampled, positions[-1])
    return sampled


@dataclass(frozen=True)
class MowerProtections:
    """Runtime protection flags from getProtectState.

    These say whether a protection is *active right now* — not whether its
    setting is enabled. Animal protection with a 21:00-08:00 window reports
    ``animal_active=False`` at midday while the setting itself is on.
    """

    animal_active: bool | None = None
    rain_active: bool | None = None
    rain_delay_active: bool | None = None
    emergency_stop: bool | None = None
    locked: bool | None = None


@dataclass(frozen=True)
class MowerTelemetry:
    """Firmware telemetry pushed via ``onFwBuryPoint-bd_*`` topics.

    Voltages arrive in millivolts, current in milliamps, temperature in C.
    """

    battery_temperature: int | None = None
    battery_level: int | None = None
    battery_current: int | None = None
    battery_voltage: int | None = None
    system_voltage: int | None = None
    motor_voltage: int | None = None
    motor_drive_voltage: int | None = None
    core_plate_voltage: int | None = None


@dataclass(frozen=True)
class MowerState:
    """Complete cached mower state used by Home Assistant entities."""

    available: bool = True
    activity: MowerActivity = MowerActivity.UNKNOWN
    # Active job type reported by cleanState.content.type: "auto" for a full
    # mow, "borderrotate" for edge trimming; None when no job is running.
    clean_type: str | None = None
    # Why the job is in its current state, straight from the mower's
    # ``trigger`` field: "app" (started by app/HA), "sched" (schedule),
    # "lowBattery" (paused to recharge, resumes by itself), "continue"
    # (resuming an interrupted job), "workComplete", "alert" (error). A
    # paused job plus this field is what separates "recharging, will carry
    # on" from "someone pressed pause".
    clean_trigger: str | None = None
    # Firmware version reported by getOta/onOta.
    firmware_version: str | None = None
    # Whether the cloud reports a pending firmware update (device updateInfo).
    firmware_update_available: bool | None = None
    task_id: str | None = None
    # Most recent finished job per kind ("auto" / "borderrotate"), tracked by
    # the coordinator and persisted alongside the map history.
    last_jobs: dict[str, MowerLastJob] = field(default_factory=dict)
    battery: int | None = None
    charging: bool | None = None
    charge_mode: str | None = None
    error_code: int | None = None
    error_description: str | None = None
    network: NetworkInfo = field(default_factory=NetworkInfo)
    settings: MowerSettings = field(default_factory=MowerSettings)
    stats: MowerStats = field(default_factory=MowerStats)
    protections: MowerProtections = field(default_factory=MowerProtections)
    telemetry: MowerTelemetry = field(default_factory=MowerTelemetry)
    map: MowerMap = field(default_factory=MowerMap)
    lifespans: dict[str, float] = field(default_factory=dict)
    robot_features: dict[str, Any] | None = None
    goat_variant: str = "unknown"
    mower_family: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)
