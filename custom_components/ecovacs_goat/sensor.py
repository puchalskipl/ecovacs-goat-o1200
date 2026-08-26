"""ECOVACS GOAT mower sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
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
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from . import EcovacsConfigEntry
from .entity import EcovacsMowerEntity
from .goat_models import variant_label
from .mower_models import MowerState
from .mower_profiles import profile_for_family


@dataclass(kw_only=True, frozen=True)
class MowerSensorDescription(SensorEntityDescription):
    """Mower sensor description."""

    value_fn: Callable[[MowerState], StateType]
    attr_fn: Callable[[MowerState], dict[str, Any] | None] | None = None


SENSORS: tuple[MowerSensorDescription, ...] = (
    MowerSensorDescription(
        key="battery_level",
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
            or state.map.zones
        )
        else None,
        attr_fn=lambda state: state.map.as_dict(),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="total_stats_area",
        translation_key="total_stats_area",
        value_fn=lambda state: _area_square_meters(state.stats.total_area),
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
        key="lifespan_blade",
        translation_key="lifespan_blade",
        value_fn=lambda state: state.lifespans.get("blade"),
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="lifespan_lens_brush",
        translation_key="lifespan_lens_brush",
        value_fn=lambda state: state.lifespans.get("lensBrush"),
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
        value_fn=lambda state: state.telemetry.battery_temperature,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="battery_current",
        translation_key="battery_current",
        value_fn=lambda state: state.telemetry.battery_current,
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        suggested_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        value_fn=lambda state: state.telemetry.battery_voltage,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        suggested_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    MowerSensorDescription(
        key="system_voltage",
        translation_key="system_voltage",
        value_fn=lambda state: state.telemetry.system_voltage,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        suggested_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    MowerSensorDescription(
        key="motor_voltage",
        translation_key="motor_voltage",
        value_fn=lambda state: state.telemetry.motor_voltage,
        native_unit_of_measurement=UnitOfElectricPotential.MILLIVOLT,
        suggested_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    MowerSensorDescription(
        key="goat_model_line",
        translation_key="goat_model_line",
        value_fn=lambda state: variant_label(state.goat_variant),
        attr_fn=lambda state: {
            "variant_id": state.goat_variant,
            "family": state.mower_family,
            "map_dialect": str(profile_for_family(state.mower_family).map_dialect),
            "experimental": profile_for_family(state.mower_family).experimental,
            "robot_features": state.robot_features or {},
        },
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


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add mower sensors."""
    entities = [
        MowerSensor(coordinator, description)
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
