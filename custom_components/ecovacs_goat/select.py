"""ECOVACS GOAT mower select entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsConfigEntry
from .entity import EcovacsMowerEntity
from .mower_messages import MOWING_EFFICIENCY_OPTIONS, OBSTACLE_AVOIDANCE_OPTIONS
from .mower_models import MowerState

@dataclass(kw_only=True, frozen=True)
class MowerSelectDescription(SelectEntityDescription):
    """Mower select description."""

    option_fn: Callable[[MowerState], str | None]


# Options are the protocol-neutral keys; Home Assistant renders them from the
# ``state`` block of each select's translations, so they read in the user's
# language instead of as raw values.
SELECTS: tuple[MowerSelectDescription, ...] = (
    MowerSelectDescription(
        key="mowing_efficiency",
        translation_key="mowing_efficiency",
        option_fn=lambda state: state.settings.mowing_efficiency,
        options=list(MOWING_EFFICIENCY_OPTIONS),
        entity_category=EntityCategory.CONFIG,
    ),
    MowerSelectDescription(
        key="obstacle_avoidance",
        translation_key="obstacle_avoidance",
        option_fn=lambda state: state.settings.obstacle_avoidance,
        options=list(OBSTACLE_AVOIDANCE_OPTIONS),
        entity_category=EntityCategory.CONFIG,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add mower select entities."""
    async_add_entities(
        MowerSelect(coordinator, description)
        for coordinator in config_entry.runtime_data.coordinators
        for description in SELECTS
    )


class MowerSelect(EcovacsMowerEntity, SelectEntity):
    """Mower select entity."""

    entity_description: MowerSelectDescription

    def __init__(
        self, coordinator, entity_description: MowerSelectDescription
    ) -> None:
        """Initialize entity."""
        self.entity_description = entity_description
        super().__init__(coordinator, entity_description.key)

    @property
    def current_option(self) -> str | None:
        """Return selected option."""
        return self.entity_description.option_fn(self.coordinator.data)

    async def async_select_option(self, option: str) -> None:
        """Select an option."""
        if self.entity_description.key == "mowing_efficiency":
            await self.coordinator.set_mowing_efficiency(option)
        elif self.entity_description.key == "obstacle_avoidance":
            await self.coordinator.set_obstacle_avoidance(option)
