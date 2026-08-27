"""Firmware update entity for the ECOVACS GOAT mower."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EcovacsConfigEntry
from .entity import EcovacsMowerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: EcovacsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the mower firmware update entity."""
    async_add_entities(
        MowerFirmwareUpdate(coordinator)
        for coordinator in config_entry.runtime_data.coordinators
    )


class MowerFirmwareUpdate(EcovacsMowerEntity, UpdateEntity):
    """Report the mower's firmware version and pending updates.

    Installation itself stays in the official app; this entity only signals
    that ECOVACS published a new firmware (``updateInfo.needUpdate`` from the
    cloud device record, refreshed at startup) and tracks the installed
    version from ``getOta``.
    """

    _attr_translation_key = "firmware"

    def __init__(self, coordinator) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator, "firmware_update")

    @property
    def _update_info(self) -> dict[str, Any]:
        """Return the cloud device updateInfo record."""
        info = self.coordinator.device.raw.get("updateInfo")
        return info if isinstance(info, dict) else {}

    @property
    def installed_version(self) -> str | None:
        """Return the firmware version the mower runs."""
        return self.coordinator.data.firmware_version

    @property
    def latest_version(self) -> str | None:
        """Return the available version, or the installed one when current."""
        installed = self.installed_version
        if not self._update_info.get("needUpdate"):
            return installed
        # The cloud flag carries no target version number; any differing
        # string flips the entity to "update available".
        return str(self._update_info.get("version") or "new version")

    @property
    def release_summary(self) -> str | None:
        """Return the firmware changelog when the cloud provides one."""
        change_log = self._update_info.get("changeLog")
        return str(change_log) if change_log else None
