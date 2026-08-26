"""ECOVACS GOAT mower number entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import DEGREE, EntityCategory, UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsConfigEntry
from .entity import EcovacsMowerEntity
from .mower_models import (
    CUT_HEIGHT_MAX_MM,
    CUT_HEIGHT_MIN_MM,
    CUT_HEIGHT_STEP_MM,
    MowerState,
    cut_height_level_from_mm,
    cut_height_mm_from_level,
)

# Volume levels run 0..total; the mower reports total = 10.
VOLUME_MAX_LEVEL = 10


def _cutting_height_mm(state: MowerState) -> int | None:
    """Return the first AreaParameters cutting height in millimetres."""
    for parameter in state.settings.area_parameters:
        if parameter.mow_height_level is not None:
            return cut_height_mm_from_level(parameter.mow_height_level)
    return None


@dataclass(kw_only=True, frozen=True)
class MowerNumberDescription(NumberEntityDescription):
    """Mower number description."""

    value_fn: Callable[[MowerState], float | None]


NUMBERS: tuple[MowerNumberDescription, ...] = (
    MowerNumberDescription(
        key="rain_delay",
        name="Rain delay",
        value_fn=lambda state: state.settings.rain_delay,
        native_min_value=0,
        native_max_value=1440,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    ),
    MowerNumberDescription(
        key="cut_direction",
        name="Cut direction",
        value_fn=lambda state: state.settings.cut_direction,
        native_min_value=0,
        native_max_value=180,
        native_step=1,
        native_unit_of_measurement=DEGREE,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
    ),
    MowerNumberDescription(
        key="volume",
        name="Volume",
        value_fn=lambda state: state.settings.volume,
        native_min_value=0,
        native_max_value=VOLUME_MAX_LEVEL,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
    ),
    MowerNumberDescription(
        key="fall_volume",
        name="Lift alarm volume",
        value_fn=lambda state: state.settings.fall_volume,
        native_min_value=0,
        native_max_value=VOLUME_MAX_LEVEL,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
    ),
    MowerNumberDescription(
        key="search_volume",
        name="Find mower volume",
        value_fn=lambda state: state.settings.search_volume,
        native_min_value=0,
        native_max_value=VOLUME_MAX_LEVEL,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
    ),
    MowerNumberDescription(
        key="cutting_height",
        name="Cutting height",
        value_fn=_cutting_height_mm,
        native_min_value=CUT_HEIGHT_MIN_MM,
        native_max_value=CUT_HEIGHT_MAX_MM,
        native_step=CUT_HEIGHT_STEP_MM,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add mower number entities."""
    async_add_entities(
        MowerNumber(coordinator, description)
        for coordinator in config_entry.runtime_data.coordinators
        for description in NUMBERS
    )


class MowerNumber(EcovacsMowerEntity, NumberEntity):
    """Mower number entity."""

    entity_description: MowerNumberDescription

    def __init__(
        self, coordinator, entity_description: MowerNumberDescription
    ) -> None:
        """Initialize entity."""
        self.entity_description = entity_description
        super().__init__(coordinator, entity_description.key)

    @property
    def native_value(self) -> float | None:
        """Return number value."""
        return self.entity_description.value_fn(self.coordinator.data)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        if self.entity_description.key == "rain_delay":
            await self.coordinator.set_rain_delay(int(value))
        elif self.entity_description.key == "cut_direction":
            await self.coordinator.set_cut_direction(int(value))
        elif self.entity_description.key in {"volume", "fall_volume", "search_volume"}:
            await self.coordinator.set_volume(
                self.entity_description.key, int(value)
            )
        elif self.entity_description.key == "cutting_height":
            parameters = self.coordinator.data.settings.area_parameters
            area_id = parameters[0].area_id if parameters else 1
            await self.coordinator.set_area_mow_height(
                area_id, cut_height_level_from_mm(value)
            )
