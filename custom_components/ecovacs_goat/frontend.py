"""Auto-register and version the bundled Lovelace card.

The integration ships its dashboard card (``frontend/ecovacs-goat-card.js``) and
serves it over HTTP so users do not have to copy the file into ``/config/www``
or add a Lovelace resource by hand. The card is registered as a Lovelace
resource (storage mode) or, failing that, as a frontend extra module, with a
hash of the card file contents as the ``?v=`` cache-busting token, so the
browser automatically fetches the new card whenever the file actually changes
(and only then) - regardless of whether the integration version was bumped.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN
from .lovelace_resource import RESOURCE_TYPE, plan_card_resource

try:
    from homeassistant.components.lovelace.const import LOVELACE_DATA
except ImportError:  # Home Assistant < 2025.2 keeps a plain dict under "lovelace"
    LOVELACE_DATA = "lovelace"

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "ecovacs-goat-card.js"
CARD_URL_PATH = f"/{DOMAIN}/{CARD_FILENAME}"
_REGISTERED_KEY = f"{DOMAIN}_frontend_registered"


async def async_register_frontend_card(hass: HomeAssistant) -> None:
    """Serve the bundled card and auto-load it (versioned for cache-busting).

    Registration is process-global and idempotent: HTTP static paths can only be
    registered once, so repeated setups (multiple entries, reloads) are no-ops.
    """
    if hass.data.get(_REGISTERED_KEY):
        return

    card_path = Path(__file__).parent / "frontend" / CARD_FILENAME
    if not await hass.async_add_executor_job(card_path.is_file):
        _LOGGER.warning("ECOVACS GOAT card asset missing at %s", card_path)
        return

    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL_PATH, str(card_path), False)]
        )
    except RuntimeError as err:
        # Already registered (e.g. a prior failed setup); treat as done.
        _LOGGER.debug("ECOVACS GOAT card static path already registered: %s", err)

    token = await _async_card_cache_token(hass, card_path)
    url = f"{CARD_URL_PATH}?v={token}"
    if await _async_register_lovelace_resource(hass, url):
        how = "Lovelace resource"
    else:
        add_extra_js_url(hass, url)
        how = "frontend module"
    hass.data[_REGISTERED_KEY] = True
    _LOGGER.info("Registered ECOVACS GOAT dashboard card at %s (%s)", url, how)


async def _async_register_lovelace_resource(hass: HomeAssistant, url: str) -> bool:
    """Load the card as a Lovelace resource; return False where that is impossible.

    ``add_extra_js_url`` alone is not enough: it imports the card from the page
    shell, before the frontend connects, and in the Android companion app a
    relaunch that served the page's scripts from the WebView cache repeatedly
    left the card undefined ("Custom element doesn't exist: ecovacs-goat-card").
    Cards loaded as Lovelace resources - fetched once the dashboard connects -
    were unaffected. Resources are only writable in storage mode; YAML mode
    keeps the extra-module path.
    """
    resources = _storage_mode_resources(hass)
    if resources is None:
        return False
    try:
        await resources.async_get_info()  # loads the collection from storage
        plan = plan_card_resource(resources.async_items(), CARD_URL_PATH, url)
        data = {"res_type": RESOURCE_TYPE, "url": url}
        if plan.create:
            await resources.async_create_item(data)
        if plan.update_id is not None:
            await resources.async_update_item(plan.update_id, data)
        for item_id in plan.delete_ids:
            await resources.async_delete_item(item_id)
    except Exception:  # noqa: BLE001 - fall back to the extra module instead
        _LOGGER.warning(
            "Could not register the ECOVACS GOAT card as a Lovelace resource; "
            "loading it as a frontend module instead",
            exc_info=True,
        )
        return False
    return True


def _storage_mode_resources(hass: HomeAssistant) -> Any | None:
    """Return the Lovelace resource collection when it is storage-managed."""
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        return None
    if isinstance(data, dict):
        mode, resources = data.get("mode"), data.get("resources")
    else:
        # 2025.2+ dataclass; ``mode`` became ``resource_mode`` in 2026.
        mode = getattr(data, "resource_mode", getattr(data, "mode", None))
        resources = getattr(data, "resources", None)
    if mode != "storage" or not hasattr(resources, "async_create_item"):
        return None
    return resources


async def _async_card_cache_token(hass: HomeAssistant, card_path: Path) -> str:
    """Return a cache-busting token derived from the card file contents.

    Hashing the file (rather than using the integration version) guarantees the
    browser refetches the card whenever the file actually changes - including
    HACS beta updates that do not bump ``manifest.json`` - and never otherwise.
    Falls back to the integration version if the file cannot be read.
    """

    def _hash() -> str:
        return hashlib.sha256(card_path.read_bytes()).hexdigest()[:12]

    try:
        return await hass.async_add_executor_job(_hash)
    except OSError:
        # Cache busting is best-effort only; fall back to the integration version.
        return await _async_card_version(hass)


async def _async_card_version(hass: HomeAssistant) -> str:
    """Return the integration version (fallback cache-bust token)."""
    try:
        integration = await async_get_integration(hass, DOMAIN)
    except Exception:  # noqa: BLE001 - version is best-effort cache busting only
        return "0"
    return str(integration.version or "0")
