"""ECOVACS GOAT lawn mower entity."""

from __future__ import annotations

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityEntityDescription,
    LawnMowerEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsConfigEntry
from .entity import EcovacsMowerEntity
from .mower_models import MowerActivity

STATE_IDLE = "idle"

# Triggers that mean the mower parked itself mid-job and will carry on
# without anyone touching it: it stopped to recharge, or it is already
# picking the job back up.
RESUMING_TRIGGERS = frozenset({"lowBattery", "continue"})

ACTIVITY_MAP = {
    MowerActivity.IDLE: STATE_IDLE,
    MowerActivity.MOWING: LawnMowerActivity.MOWING,
    MowerActivity.PAUSED: LawnMowerActivity.PAUSED,
    MowerActivity.RETURNING: LawnMowerActivity.RETURNING,
    MowerActivity.DOCKED: LawnMowerActivity.DOCKED,
    MowerActivity.ERROR: LawnMowerActivity.ERROR,
    MowerActivity.UNKNOWN: None,
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the ECOVACS mowers."""
    async_add_entities(
        EcovacsMower(coordinator)
        for coordinator in config_entry.runtime_data.coordinators
    )


class EcovacsMower(EcovacsMowerEntity, LawnMowerEntity):
    """ECOVACS GOAT mower."""

    _attr_supported_features = (
        LawnMowerEntityFeature.DOCK
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.START_MOWING
    )
    entity_description = LawnMowerEntityEntityDescription(key="mower", name=None)

    def __init__(self, coordinator) -> None:
        """Initialize the mower entity."""
        super().__init__(coordinator, "mower")

    @property
    def activity(self) -> LawnMowerActivity | str | None:
        """Return mower activity."""
        return ACTIVITY_MAP[self.coordinator.data.activity]

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | None]:
        """Expose the active job, why it is in this state, and whether it resumes.

        A mower parked mid-job to recharge reports the same "paused" activity
        as one a person paused, so ``pause_reason`` carries the mower's own
        ``trigger``, ``resumes_automatically`` answers the question a dashboard
        actually wants (will this carry on by itself?), and ``charging`` says
        whether it is actually drawing charge.
        """
        data = self.coordinator.data
        trigger = data.clean_trigger
        return {
            "work_mode": data.clean_type,
            # Reported by the mower rather than guessed from "docked and not
            # full": a mower can sit on the dock without drawing charge.
            "charging": data.charging,
            "pause_reason": trigger if data.activity is MowerActivity.PAUSED else None,
            "resumes_automatically": (
                trigger in RESUMING_TRIGGERS
                if data.activity is MowerActivity.PAUSED
                else None
            ),
        }

    async def async_start_mowing(self) -> None:
        """Start or resume mowing."""
        await self.coordinator.start_mowing()

    async def async_pause(self) -> None:
        """Pause mowing."""
        await self.coordinator.pause()

    async def async_dock(self) -> None:
        """Return to dock/charge."""
        await self.coordinator.dock()
