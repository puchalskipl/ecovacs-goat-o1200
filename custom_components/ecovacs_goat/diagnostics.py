"""Ecovacs diagnostics."""

from __future__ import annotations

from functools import partial
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_DEVICE_ID, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import EcovacsConfigEntry
from .const import CONF_ACCESS_TOKEN, CONF_ACCOUNT_UID, CONF_SESSION_STORE_ID

REDACT_CONFIG = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_DEVICE_ID,
    CONF_SESSION_STORE_ID,
    CONF_ACCOUNT_UID,
    CONF_ACCESS_TOKEN,
    "title",
}
REDACT_DEVICE = {"did", "name", "nick", "homeId"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: EcovacsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    controller = config_entry.runtime_data
    diag: dict[str, Any] = {
        "config": async_redact_data(config_entry.as_dict(), REDACT_CONFIG)
    }

    diag["devices"] = [
        async_redact_data(device, REDACT_DEVICE)
        for device in controller.devices
    ]
    diag["protocol_profiles"] = [
        coordinator.protocol_profile for coordinator in controller.coordinators
    ]
    diag["debug_capture"] = {
        "summary": controller.debug_capture.summary(),
        # Reads event files from disk — keep it off the event loop.
        "recent_events": await hass.async_add_executor_job(
            partial(controller.debug_capture.recent_events, limit=100)
        ),
    }

    return diag
