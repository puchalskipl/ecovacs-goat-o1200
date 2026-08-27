"""ECOVACS GOAT mower sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_DESCRIPTION,
    PERCENTAGE,
    EntityCategory,
    UnitOfArea,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import EcovacsConfigEntry
from .entity import EcovacsMowerEntity
from .mower_models import MowerState


@dataclass(kw_only=True, frozen=True)
class MowerSensorDescription(SensorEntityDescription):
    """Mower sensor description."""

    value_fn: Callable[[MowerState], StateType]
    attr_fn: Callable[[MowerState], dict[str, Any] | None] | None = None
    # Restore the last value after a restart/reload. Used for telemetry that
    # the mower only pushes while the battery is charging or discharging —
    # without this, those sensors sit on "unknown" for hours after a restart.
    restore: bool = False


SENSORS: tuple[MowerSensorDescription, ...] = (
    MowerSensorDescription(
        key="battery_level",
        suggested_display_precision=0,
        translation_key="battery_level",
        value_fn=lambda state: state.battery,
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="error",
        translation_key="error",
        value_fn=lambda state: state.error_code,
        attr_fn=lambda state: {CONF_DESCRIPTION: state.error_description},
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="network_ip",
        translation_key="network_ip",
        value_fn=lambda state: state.network.ip,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="network_rssi",
        translation_key="network_rssi",
        value_fn=lambda state: state.network.rssi,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="network_ssid",
        translation_key="network_ssid",
        value_fn=lambda state: state.network.ssid,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="stats_area",
        translation_key="stats_area",
        value_fn=lambda state: _area_square_meters(state.stats.area),
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        device_class=SensorDeviceClass.AREA,
    ),
    MowerSensorDescription(
        key="stats_job_area",
        translation_key="stats_job_area",
        value_fn=lambda state: _area_square_meters(state.stats.job_area),
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        device_class=SensorDeviceClass.AREA,
    ),
    MowerSensorDescription(
        key="stats_progress",
        suggested_display_precision=0,
        translation_key="stats_progress",
        value_fn=lambda state: state.stats.progress,
        native_unit_of_measurement=PERCENTAGE,
    ),
    MowerSensorDescription(
        key="stats_time",
        translation_key="stats_time",
        value_fn=lambda state: state.stats.duration,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
    ),
    MowerSensorDescription(
        key="live_map",
        translation_key="live_map",
        value_fn=lambda state: "live"
        if (
            state.map.current_position
            or state.map.trace.chunks
            or state.map.trace.path
            or state.map.position_history
            or state.map.info.obstacles
        )
        else None,
        attr_fn=lambda state: state.map.as_dict(),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="total_stats_area",
        translation_key="total_stats_area",
        suggested_display_precision=0,
        # getTotalStats reports area in m2 already, unlike onStats (cm2).
        value_fn=lambda state: state.stats.total_area,
        native_unit_of_measurement=UnitOfArea.SQUARE_METERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.AREA,
    ),
    MowerSensorDescription(
        key="total_stats_time",
        translation_key="total_stats_time",
        value_fn=lambda state: state.stats.total_duration,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        device_class=SensorDeviceClass.DURATION,
    ),
    MowerSensorDescription(
        key="total_stats_cleanings",
        translation_key="total_stats_cleanings",
        value_fn=lambda state: state.stats.total_count,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    MowerSensorDescription(
        key="last_mowing",
        translation_key="last_mowing",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda state: _last_job_ended(state, "auto"),
        attr_fn=lambda state: _last_job_attributes(state, "auto"),
    ),
    MowerSensorDescription(
        key="last_edge_trim",
        translation_key="last_edge_trim",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda state: _last_job_ended(state, "borderrotate"),
        attr_fn=lambda state: _last_job_attributes(state, "borderrotate"),
    ),
    MowerSensorDescription(
        key="lifespan_blade",
        suggested_display_precision=0,
        translation_key="lifespan_blade",
        value_fn=lambda state: state.lifespans.get("blade"),
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="protection_state",
        translation_key="protection_state",
        value_fn=lambda state: _active_protections(state),
        attr_fn=lambda state: {
            "animal_protection_active": state.protections.animal_active,
            "rain_protection_active": state.protections.rain_active,
            "rain_delay_active": state.protections.rain_delay_active,
            "emergency_stop": state.protections.emergency_stop,
            "locked": state.protections.locked,
        },
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="battery_temperature",
        translation_key="battery_temperature",
        restore=True,
        value_fn=lambda state: state.telemetry.battery_temperature,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


def _active_protections(state: MowerState) -> str | None:
    """Summarise which protections are currently holding the mower back."""
    protections = state.protections
    active = [
        label
        for label, value in (
            ("animal", protections.animal_active),
            ("rain", protections.rain_active),
            ("rain_delay", protections.rain_delay_active),
            ("emergency_stop", protections.emergency_stop),
            ("locked", protections.locked),
        )
        if value
    ]
    if not active:
        return (
            "none"
            if protections != type(protections)()
            else None
        )
    return ", ".join(active)


def _area_square_meters(value: int | None) -> float | None:
    """Convert ECOVACS area values from cm2 to m2 for HA unit conversion."""
    return None if value is None else value / 10000


def _last_job_ended(state: MowerState, kind: str) -> Any:
    """Return when the last job of the given kind finished."""
    job = state.last_jobs.get(kind)
    if job is None or not job.ended_at:
        return None
    return dt_util.parse_datetime(job.ended_at)


def _last_job_attributes(state: MowerState, kind: str) -> dict[str, Any] | None:
    """Expose the last job's details alongside its finish timestamp."""
    job = state.last_jobs.get(kind)
    if job is None:
        return None
    return {
        "started_at": job.started_at,
        "mowed_area_m2": job.mowed_area,
        "duration_minutes": job.duration_minutes,
        "task_id": job.task_id,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add mower sensors."""
    entities = [
        RestoringMowerSensor(coordinator, description)
        if description.restore
        else MowerSensor(coordinator, description)
        for coordinator in config_entry.runtime_data.coordinators
        for description in SENSORS
    ]
    entities.extend(
        DebugCaptureSensor(coordinator)
        for coordinator in config_entry.runtime_data.coordinators
    )
    async_add_entities(entities)


class MowerSensor(EcovacsMowerEntity, SensorEntity):
    """Mower sensor."""

    entity_description: MowerSensorDescription

    def __init__(
        self, coordinator, entity_description: MowerSensorDescription
    ) -> None:
        """Initialize sensor."""
        self.entity_description = entity_description
        super().__init__(coordinator, entity_description.key)

    @property
    def native_value(self) -> StateType:
        """Return native value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes."""
        if self.entity_description.attr_fn is None:
            return None
        return self.entity_description.attr_fn(self.coordinator.data)


class RestoringMowerSensor(MowerSensor, RestoreSensor):
    """Mower sensor that falls back to its last value after a restart.

    Battery telemetry only streams while the battery charges or discharges,
    so after a restart the last known value beats hours of "unknown".
    """

    _restored_value: StateType = None

    async def async_added_to_hass(self) -> None:
        """Restore the previous native value."""
        await super().async_added_to_hass()
        data = await self.async_get_last_sensor_data()
        if data is not None:
            self._restored_value = data.native_value

    @property
    def native_value(self) -> StateType:
        """Return the live value, falling back to the restored one."""
        value = self.entity_description.value_fn(self.coordinator.data)
        return value if value is not None else self._restored_value


class DebugCaptureSensor(EcovacsMowerEntity, SensorEntity):
    """Expose debug capture status and download URL."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "debug_capture"

    def __init__(self, coordinator) -> None:
        """Initialize debug capture sensor."""
        super().__init__(coordinator, "debug_capture")

    @property
    def native_value(self) -> str:
        """Return capture status."""
        return "active" if self.coordinator.debug_capture.is_active else "inactive"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return capture summary and latest download URL."""
        summary = self.coordinator.debug_capture.summary()
        last_export = summary.get("last_export") or {}
        return {
            "active_session": summary.get("active"),
            "retained_sessions": summary.get("sessions", []),
            "latest_download_url": last_export.get("url"),
            "latest_export_session": last_export.get("session_id"),
        }
