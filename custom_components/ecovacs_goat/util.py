"""Ecovacs util functions."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import secrets
import string
from typing import Any, cast

CONF_DEVICE_ID = "device_id"
CONF_SESSION_STORE_ID = "session_store_id"

CLIENT_DEVICE_ID_LENGTH = 8
CLIENT_DEVICE_ID_ALPHABET = string.ascii_uppercase + string.digits
SESSION_STORE_ID_LENGTH = 32
SESSION_STORE_ID_ALPHABET = string.hexdigits.lower()[:16]


def generate_client_device_id() -> str:
    """Return a new ECOVACS-style client device id."""
    return "".join(
        secrets.choice(CLIENT_DEVICE_ID_ALPHABET)
        for _ in range(CLIENT_DEVICE_ID_LENGTH)
    )


def generate_session_store_id() -> str:
    """Return an opaque id for the private account-session store."""
    return secrets.token_hex(SESSION_STORE_ID_LENGTH // 2)


def is_valid_client_device_id(device_id: Any) -> bool:
    """Return whether a value matches ECOVACS' client-device id format."""
    return (
        isinstance(device_id, str)
        and len(device_id) == CLIENT_DEVICE_ID_LENGTH
        and all(character in CLIENT_DEVICE_ID_ALPHABET for character in device_id)
    )


def is_valid_session_store_id(store_id: Any) -> bool:
    """Return whether a private-store id is safe to use in a storage key."""
    return (
        isinstance(store_id, str)
        and len(store_id) == SESSION_STORE_ID_LENGTH
        and all(character in SESSION_STORE_ID_ALPHABET for character in store_id)
    )


def get_client_device_id(config: Mapping[str, Any] | None = None) -> str:
    """Return a persisted client device id, or generate one if missing."""
    if config and is_valid_client_device_id(config.get(CONF_DEVICE_ID)):
        return cast(str, config[CONF_DEVICE_ID])
    return generate_client_device_id()


def get_session_store_id(config: Mapping[str, Any] | None = None) -> str:
    """Return a persisted private-store id, or generate one if missing."""
    if config and is_valid_session_store_id(config.get(CONF_SESSION_STORE_ID)):
        return cast(str, config[CONF_SESSION_STORE_ID])
    return generate_session_store_id()


def account_fingerprint(username: str, country: str, client_device_id: str) -> str:
    """Return a non-reversible binding for the configured account identity."""
    material = "\0".join(
        (username.strip().casefold(), country.upper(), client_device_id)
    )
    return hashlib.sha256(material.encode()).hexdigest()
