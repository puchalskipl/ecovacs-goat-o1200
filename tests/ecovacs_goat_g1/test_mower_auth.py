"""Tests for ECOVACS device-verification authentication."""

from __future__ import annotations

from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

PACKAGE_PATH = Path(__file__).parents[2] / "custom_components" / "ecovacs_goat_g1"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(PACKAGE_PATH.parent)]
sys.modules.setdefault("custom_components", custom_components)

ecovacs_goat_g1 = types.ModuleType("custom_components.ecovacs_goat_g1")
ecovacs_goat_g1.__path__ = [str(PACKAGE_PATH)]
sys.modules.setdefault("custom_components.ecovacs_goat_g1", ecovacs_goat_g1)

from custom_components.ecovacs_goat_g1.mower_api import (
    META,
    PRIVATE_API_PATH_FORMAT,
    AccountTokens,
    DeviceVerificationRequiredError,
    EcovacsAuthError,
    EcovacsMowerApi,
    InvalidVerificationCodeError,
    _load_public_key,
)
from custom_components.ecovacs_goat_g1.util import (
    generate_client_device_id,
    get_client_device_id,
)


def test_app_version_is_current() -> None:
    """Password login must advertise a current ECOVACS HOME version."""
    assert META["appVersion"] == "3.14.0"
    login_path = PRIVATE_API_PATH_FORMAT.format(
        country="us",
        lang="EN",
        deviceId="ABCDEF12",
        appCode="global_e",
        appVersion=META["appVersion"],
        channel="google_play",
        deviceType="1",
        endpoint="user/login",
    )
    assert "/global_e/3.14.0/google_play/1/user/login" in login_path
    assert "1.6.3" not in login_path


def test_client_device_id_is_stable_when_persisted() -> None:
    """A stored device id is reused; a missing one is generated once."""
    assert get_client_device_id({"device_id": "ABCDEF12"}) == "ABCDEF12"
    first = generate_client_device_id()
    second = generate_client_device_id()
    assert len(first) == 8
    assert first.isalnum()
    assert first.isupper() or any(ch.isdigit() for ch in first)
    assert first != second


def _api() -> EcovacsMowerApi:
    return EcovacsMowerApi(
        MagicMock(),
        username="user@example.com",
        password="secret",
        country="US",
        device_id="ABCDEF12",
    )


@pytest.mark.asyncio
async def test_auth_error_codes() -> None:
    """Map ECOVACS auth codes to the matching exception types."""
    api = _api()
    api._session.get = MagicMock()

    async def _failed(code: str):
        response = AsyncMock()
        response.raise_for_status = MagicMock()
        response.json = AsyncMock(
            return_value={"code": code, "msg": "nope", "data": None, "success": False}
        )
        response.__aenter__.return_value = response
        response.__aexit__.return_value = False
        api._session.get.return_value = response
        return await api._signed_get("https://example.test", {}, {}, "k", "s")

    with pytest.raises(EcovacsAuthError, match="invalid credentials"):
        await _failed("1005")
    with pytest.raises(InvalidVerificationCodeError):
        await _failed("1012")
    with pytest.raises(DeviceVerificationRequiredError):
        await _failed("1013")


@pytest.mark.asyncio
async def test_authenticate_uses_stored_tokens_instead_of_password() -> None:
    """Setup after verification must not call the password login again."""
    from custom_components.ecovacs_goat_g1.mower_api import Credentials

    api = EcovacsMowerApi(
        MagicMock(),
        username="user@example.com",
        password="secret",
        country="US",
        device_id="ABCDEF12",
        account_tokens=AccountTokens(user_id="uid-long", access_token="access-1"),
    )
    api._login_password = AsyncMock(side_effect=AssertionError("password login"))
    api._complete_login = AsyncMock(
        return_value=Credentials(
            user_id="uid-short", token="portal-token", expires_at=9_999_999_999
        )
    )

    credentials = await api.authenticate()

    api._login_password.assert_not_called()
    api._complete_login.assert_awaited_once_with("uid-long", "access-1")
    assert credentials.token == "portal-token"
    assert api.account_tokens == AccountTokens("uid-long", "access-1")


def test_load_public_key_roundtrip() -> None:
    """The ECOVACS getConfig public-key blob loads as an RSA key."""
    import base64
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    loaded = _load_public_key(json.dumps({"publicKey": base64.b64encode(der).decode()}))
    assert loaded.key_size == 2048
