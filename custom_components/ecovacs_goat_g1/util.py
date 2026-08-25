"""Ecovacs util functions."""

from __future__ import annotations

from collections.abc import Mapping
import random
import string
from typing import Any, cast

CONF_DEVICE_ID = "device_id"


def generate_client_device_id() -> str:
    """Return a new ECOVACS-style client device id."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(8))


def get_client_device_id(config: Mapping[str, Any] | None = None) -> str:
    """Return a persisted client device id, or generate one if missing."""
    if config and (device_id := config.get(CONF_DEVICE_ID)):
        return cast(str, device_id)
    return generate_client_device_id()
