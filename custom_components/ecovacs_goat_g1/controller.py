"""Controller for the mower-only ECOVACS integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_COUNTRY,
    CONF_DEVICE_ID,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_UID,
    DEFAULT_DEBUG_CAPTURE_MAX_DURATION_MINUTES,
    DEFAULT_DEBUG_CAPTURE_MAX_SIZE_MB,
    DEFAULT_DEBUG_CAPTURE_RAW_PAYLOADS,
    OPTION_DEBUG_CAPTURE_MAX_DURATION_MINUTES,
    OPTION_DEBUG_CAPTURE_MAX_SIZE_MB,
    OPTION_DEBUG_CAPTURE_RAW_PAYLOADS,
)
from .debug_capture import DebugCaptureStore
from .mower_api import (
    AccountTokens,
    DeviceVerificationRequiredError,
    EcovacsAuthError,
    EcovacsMowerApi,
)
from .mower_coordinator import MowerCoordinator
from .util import get_client_device_id

_LOGGER = logging.getLogger(__name__)


class EcovacsController:
    """Mower-only ECOVACS controller."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize controller."""
        self._hass = hass
        self._entry = entry
        config: Mapping[str, Any] = entry.data
        self._configured_name = str(config.get(CONF_NAME) or "Ecovacs-GOAT")
        self._debug_capture = DebugCaptureStore(
            Path(hass.config.path("ecovacs_goat_g1_debug")),
            Path(hass.config.path("www", "ecovacs_goat", "debug")),
        )
        self._configure_debug_capture(entry.options)
        device_id = get_client_device_id(config)
        if CONF_DEVICE_ID not in config:
            hass.config_entries.async_update_entry(
                entry, data={**dict(config), CONF_DEVICE_ID: device_id}
            )
        account_tokens = None
        if config.get(CONF_ACCOUNT_UID) and config.get(CONF_ACCESS_TOKEN):
            account_tokens = AccountTokens(
                user_id=str(config[CONF_ACCOUNT_UID]),
                access_token=str(config[CONF_ACCESS_TOKEN]),
            )
        for value in (
            config.get(CONF_USERNAME),
            config.get(CONF_PASSWORD),
            config.get(CONF_ACCESS_TOKEN),
            config.get(CONF_ACCOUNT_UID),
            self._configured_name,
            device_id,
        ):
            self._debug_capture.add_redaction_value(value)
        self._api = EcovacsMowerApi(
            aiohttp_client.async_get_clientsession(hass),
            username=config[CONF_USERNAME],
            password=config[CONF_PASSWORD],
            country=config[CONF_COUNTRY],
            device_id=device_id,
            debug_capture=self._debug_capture,
            account_tokens=account_tokens,
        )
        self._coordinators: list[MowerCoordinator] = []

    async def initialize(self) -> None:
        """Initialize mower devices and coordinators."""
        started: list[MowerCoordinator] = []
        try:
            await self._api.authenticate()
            self._persist_auth_data()
            devices = await self._api.get_devices()
            if not devices:
                raise ConfigEntryNotReady("No ECOVACS mower devices found")

            for device in devices:
                device = replace(device, name=self._configured_name)
                for value in (
                    device.did,
                    device.device_class,
                    device.resource,
                    device.name,
                    device.model,
                ):
                    self._debug_capture.add_redaction_value(value)
                coordinator = MowerCoordinator(
                    self._hass,
                    self._api,
                    device,
                    self._debug_capture,
                )
                await coordinator.async_start()
                started.append(coordinator)
                _LOGGER.info("Initialized ECOVACS mower %s", device.name)
            self._coordinators = started
        except DeviceVerificationRequiredError as ex:
            raise ConfigEntryAuthFailed(
                "ECOVACS device verification required"
            ) from ex
        except EcovacsAuthError as ex:
            raise ConfigEntryAuthFailed("Invalid ECOVACS credentials") from ex
        except ConfigEntryNotReady:
            await self._stop_coordinators(started)
            raise
        except Exception as ex:
            await self._stop_coordinators(started)
            raise ConfigEntryNotReady("Error during ECOVACS mower setup") from ex

    async def teardown(self) -> None:
        """Disconnect controller."""
        await self._stop_coordinators(self._coordinators)
        self._coordinators.clear()

    async def _stop_coordinators(
        self, coordinators: list[MowerCoordinator]
    ) -> None:
        """Stop any coordinators that were already started."""
        for coordinator in coordinators:
            await coordinator.async_stop()

    @property
    def coordinators(self) -> list[MowerCoordinator]:
        """Return mower coordinators."""
        return self._coordinators

    @property
    def devices(self) -> list[dict[str, Any]]:
        """Return raw device info for diagnostics."""
        return [coordinator.device.raw for coordinator in self._coordinators]

    @property
    def debug_capture(self) -> DebugCaptureStore:
        """Return debug capture store."""
        return self._debug_capture

    def _persist_auth_data(self) -> None:
        """Store the verified client id and account tokens on the entry."""
        tokens = self._api.account_tokens
        new_data = {
            **self._entry.data,
            CONF_DEVICE_ID: self._api.client_device_id,
        }
        if tokens is not None:
            new_data[CONF_ACCOUNT_UID] = tokens.user_id
            new_data[CONF_ACCESS_TOKEN] = tokens.access_token
        if new_data != dict(self._entry.data):
            self._hass.config_entries.async_update_entry(self._entry, data=new_data)

    def _configure_debug_capture(self, options: Mapping[str, Any]) -> None:
        """Apply capture defaults from config entry options."""
        self._debug_capture.configure(
            include_raw_payloads=bool(
                options.get(
                    OPTION_DEBUG_CAPTURE_RAW_PAYLOADS,
                    DEFAULT_DEBUG_CAPTURE_RAW_PAYLOADS,
                )
            ),
            max_duration_seconds=int(
                options.get(
                    OPTION_DEBUG_CAPTURE_MAX_DURATION_MINUTES,
                    DEFAULT_DEBUG_CAPTURE_MAX_DURATION_MINUTES,
                )
            )
            * 60,
            max_bytes=int(
                options.get(
                    OPTION_DEBUG_CAPTURE_MAX_SIZE_MB,
                    DEFAULT_DEBUG_CAPTURE_MAX_SIZE_MB,
                )
            )
            * 1024
            * 1024,
        )
