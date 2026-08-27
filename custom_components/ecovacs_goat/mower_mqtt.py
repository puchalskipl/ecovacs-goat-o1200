"""MQTT push client for ECOVACS GOAT mowers."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import json
import logging
import socket
import ssl
from time import monotonic
from typing import Any

import paho.mqtt.client as mqtt

from .debug_capture import DebugCaptureStore
from .mower_api import EcovacsMowerApi
from .mower_models import MowerDevice

_LOGGER = logging.getLogger(__name__)
APP_PRESENCE_FEATURE_META = {"fv": "1.0.0", "wv": "v2.1.0"}
APP_PRESENCE_ROLE_META = {"app": "user", "st": 10}
# Reconnect backoff for the push client after an unexpected disconnect.
RECONNECT_INITIAL_DELAY_SECONDS = 60
RECONNECT_MAX_DELAY_SECONDS = 600

# Whether a broker passed certificate verification, cached per host so the
# probe runs once per HA start.
_TLS_VERIFY_CACHE: dict[str, bool] = {}


def _probe_tls_verification(host: str, port: int) -> bool:
    """Return whether the broker's certificate passes default verification."""
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=host):
                return True
    except ssl.SSLError:
        return False
    except OSError:
        # Network problem, not a certificate problem — prefer verification;
        # the subsequent connect will fail and be retried either way.
        return True


def _tls_context_for(host: str, port: int) -> ssl.SSLContext:
    """Return a TLS context for the broker, verified whenever possible.

    A token used as the MQTT password is a full account credential, so an
    unverified connection is only a last resort for brokers whose certificate
    genuinely fails validation — and it is logged loudly.
    """
    verify = _TLS_VERIFY_CACHE.get(host)
    if verify is None:
        verify = _probe_tls_verification(host, port)
        _TLS_VERIFY_CACHE[host] = verify
        if not verify:
            _LOGGER.warning(
                "ECOVACS MQTT broker %s failed certificate verification; "
                "falling back to an unverified TLS connection",
                host,
            )
    if verify:
        return ssl.create_default_context()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class MowerMqttClient:
    """Small paho-based MQTT client for ECOVACS push messages."""

    def __init__(
        self,
        api: EcovacsMowerApi,
        device: MowerDevice,
        loop: asyncio.AbstractEventLoop,
        on_message: Callable[[str, bytes], None],
        debug_capture: DebugCaptureStore | None = None,
    ) -> None:
        self._api = api
        self._device = device
        self._loop = loop
        self._on_message = on_message
        self._debug_capture = debug_capture
        self._client: mqtt.Client | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        """Connect and subscribe to mower MQTT push topics."""
        self._stopped = False
        credentials = await self._api.authenticate()
        client_id = f"{credentials.user_id}@ecouser/{self._api.client_device_id}"
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        client.username_pw_set(credentials.user_id, credentials.token)

        host = f"mq-{self._api.continent}.ecouser.net"
        port = 443
        ssl_context = await self._loop.run_in_executor(
            None, _tls_context_for, host, port
        )
        client.tls_set_context(ssl_context)

        client.on_connect = self._on_connect
        client.on_message = self._on_paho_message
        client.on_disconnect = self._on_disconnect
        self._client = client

        await self._loop.run_in_executor(None, client.connect, host, port, 60)
        client.loop_start()

    async def stop(self) -> None:
        """Disconnect MQTT."""
        self._stopped = True
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        await self._async_shutdown_client()

    async def _async_shutdown_client(self) -> None:
        if self._client is None:
            return
        client = self._client
        self._client = None
        await self._loop.run_in_executor(None, client.disconnect)
        # loop_stop joins the paho thread — keep that off the event loop too.
        await self._loop.run_in_executor(None, client.loop_stop)

    def _schedule_reconnect(self) -> None:
        """Arrange a credential-refreshing reconnect (event loop only)."""
        if self._stopped:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = self._loop.create_task(self._async_reconnect())

    async def _async_reconnect(self) -> None:
        """Rebuild the client with fresh credentials until push works again.

        paho's built-in reconnect reuses the password from connect time; once
        the portal token rotates the broker rejects it forever, silently
        killing all pushes. Give paho a grace period first, then rebuild.
        """
        delay = RECONNECT_INITIAL_DELAY_SECONDS
        while not self._stopped:
            await asyncio.sleep(delay)
            if self._stopped:
                return
            client = self._client
            if client is not None and client.is_connected():
                return
            _LOGGER.info(
                "ECOVACS MQTT still disconnected; rebuilding with fresh credentials"
            )
            try:
                await self._async_shutdown_client()
                await self.start()
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - retry with backoff
                _LOGGER.warning("ECOVACS MQTT reconnect failed: %s", err)
                delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)

    def _on_connect(
        self,
        client: mqtt.Client,
        _userdata: Any,
        _flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if reason_code != 0:
            _LOGGER.warning("ECOVACS MQTT connect failed: %s", reason_code)
            return

        path = f"{self._device.did}/{self._device.device_class}/{self._device.resource}"
        topics = [
            f"iot/atr/+/{path}/j",
        ]
        for topic in topics:
            client.subscribe(topic)
            _LOGGER.debug("Subscribed to ECOVACS MQTT topic %s", topic)

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if reason_code != 0:
            _LOGGER.warning("ECOVACS MQTT disconnected: %s", reason_code)
            self._loop.call_soon_threadsafe(self._schedule_reconnect)

    def _on_paho_message(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        topic = str(message.topic)
        payload = bytes(message.payload)
        _LOGGER.debug("ECOVACS MQTT message topic=%s payload=%s", topic, payload)
        if self._debug_capture is not None:
            self._debug_capture.capture_event(
                "mqtt_message",
                {
                    "topic": topic,
                    "payload_size": len(payload),
                    "payload": payload,
                    "device": {
                        "did": self._device.did,
                        "class": self._device.device_class,
                        "resource": self._device.resource,
                        "model": self._device.model,
                    },
                },
            )
        self._loop.call_soon_threadsafe(self._on_message, topic, payload)


class MowerAppPresenceMqttClient:
    """Short-lived MQTT session that mimics the official app's startup presence.

    The official Android app opens this N-GIoT user MQTT connection before the
    map screen is shown. Keeping a matching session open while the custom card
    is visible may be the cloud-side hint that enables fast position pushes.
    """

    def __init__(
        self,
        api: EcovacsMowerApi,
        device: MowerDevice,
        loop: asyncio.AbstractEventLoop,
        debug_capture: DebugCaptureStore | None = None,
    ) -> None:
        self._api = api
        self._device = device
        self._loop = loop
        self._debug_capture = debug_capture
        self._client: mqtt.Client | None = None
        self._started_at: float | None = None

    @property
    def connected(self) -> bool:
        """Return whether the app-presence client has been started."""
        return self._client is not None

    async def start(self) -> None:
        """Open the app-style N-GIoT user MQTT session if needed."""
        if self._client is not None:
            return

        credentials = await self._api.authenticate()
        realm = _jwt_claim(credentials.token, "r")
        if not realm:
            raise RuntimeError("Could not determine ECOVACS N-GIoT realm from token")

        client_id = f"{credentials.user_id}@USER/{realm}"
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(_app_presence_username(self._device), credentials.token)

        host = f"jmq-ngiot-{self._api.continent}.dc.robotww.ecouser.net"
        port = 443
        ssl_context = await self._loop.run_in_executor(
            None, _tls_context_for, host, port
        )
        client.tls_set_context(ssl_context)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client

        _LOGGER.info(
            "Starting ECOVACS app-presence MQTT session host=%s client_id=%s",
            host,
            _redact_client_id(client_id),
        )
        await self._loop.run_in_executor(None, client.connect, host, port, 60)
        client.loop_start()
        self._started_at = monotonic()
        self._capture_event(
            "app_presence_mqtt_start",
            {
                "host": host,
                "port": port,
                "client_id_shape": "<uid>@USER/<realm>",
                "device": {
                    "did": self._device.did,
                    "class": self._device.device_class,
                    "resource": self._device.resource,
                    "model": self._device.model,
                },
            },
        )

    async def stop(self) -> None:
        """Close the app-style N-GIoT user MQTT session."""
        if self._client is None:
            return
        client = self._client
        self._client = None
        started_at = self._started_at
        self._started_at = None
        await self._loop.run_in_executor(None, client.disconnect)
        await self._loop.run_in_executor(None, client.loop_stop)
        _LOGGER.info("Stopped ECOVACS app-presence MQTT session")
        self._capture_event(
            "app_presence_mqtt_stop",
            {
                "duration_seconds": round(monotonic() - started_at, 1)
                if started_at is not None
                else None,
            },
        )

    def _on_connect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if reason_code != 0:
            _LOGGER.warning(
                "ECOVACS app-presence MQTT connect failed: %s", reason_code
            )
            self._capture_event(
                "app_presence_mqtt_connect_failed", {"reason_code": str(reason_code)}
            )
            return
        _LOGGER.info("ECOVACS app-presence MQTT connected")
        self._capture_event("app_presence_mqtt_connected", {})

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if reason_code != 0:
            _LOGGER.warning(
                "ECOVACS app-presence MQTT disconnected: %s", reason_code
            )
            self._capture_event(
                "app_presence_mqtt_disconnected", {"reason_code": str(reason_code)}
            )

    def _on_message(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        topic = str(message.topic)
        payload = bytes(message.payload)
        _LOGGER.debug(
            "ECOVACS app-presence MQTT message topic=%s payload_size=%s",
            topic,
            len(payload),
        )
        self._capture_event(
            "app_presence_mqtt_message",
            {
                "topic": topic,
                "payload_size": len(payload),
                "payload": payload,
            },
        )

    def _capture_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._debug_capture is not None:
            self._debug_capture.capture_event(event_type, payload)


def _app_presence_username(device: MowerDevice) -> str:
    """Return the N-GIoT username shape captured from the Android app."""
    return (
        f"{device.did}`{_base64_json(APP_PRESENCE_FEATURE_META)}"
        f"\n`{_base64_json(APP_PRESENCE_ROLE_META)}"
    )


def _base64_json(value: dict[str, Any]) -> str:
    return base64.b64encode(
        json.dumps(value, separators=(",", ":")).encode()
    ).decode()


def _jwt_claim(token: str, claim: str) -> str | None:
    """Return a claim from an ECOVACS JWT without verifying the signature."""
    try:
        payload = token.split(".", 2)[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    value = data.get(claim)
    return str(value) if value is not None else None


def _redact_client_id(client_id: str) -> str:
    """Return a log-safe app-presence client id shape."""
    prefix, _, suffix = client_id.partition("@")
    tail = prefix[-4:] if len(prefix) >= 4 else prefix
    return f"<uid:...{tail}>@{suffix}" if suffix else "<redacted>"
