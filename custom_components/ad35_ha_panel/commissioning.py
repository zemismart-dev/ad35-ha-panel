"""CA-authenticated, narrowly scoped HA commissioning transport."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import ssl

import aiohttp

from .const import COMMISSIONING_PORT, COMMISSIONING_TRANSPORT, MIN_COMMISSIONING_FIRMWARE, MODEL, SELECTABLE_DOMAINS
from .protocol import (BASE_FIELDS, CAPABILITY, ENTITY, PanelError, decode_reply,
                       normalize_mac, normalize_origin, strict_json, validate_host)

MAX_RESPONSE = 24 * 1024
INFO_FIELDS = {"mac", "model", "configured", "busy", "name", "friendly_name", "version", "commissioning"}
BINDING_FIELDS = {"mac", "model", "ha_origin", "binding_id", "revision", "entity_ids", "weather_id"}
SAFE_ERRORS = {"commissioning_closed", "invalid_credential", "transport_mismatch", "not_ready"}


def validate_token(value):
    if not isinstance(value, str) or CAPABILITY.fullmatch(value) is None:
        raise PanelError("invalid_credential")
    return value


def validate_selection(entities, weather):
    if (not isinstance(entities, list) or len(entities) > 64
            or any(not isinstance(v, str) or len(v) > 255 or not ENTITY.fullmatch(v)
                   or v.split(".")[0] not in SELECTABLE_DOMAINS for v in entities)
            or len(set(entities)) != len(entities)
            or not isinstance(weather, str) or len(weather) > 255
            or (weather and (not ENTITY.fullmatch(weather) or not weather.startswith("weather.")))):
        raise PanelError("invalid_response")


def decode_commissioning(raw, operation, mac):
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE:
            raise ValueError
        data = strict_json(raw)
        if not isinstance(data, dict):
            raise ValueError
        if data.get("ok") is False:
            # Failed info/binding contain only the shared envelope. Validate it
            # before mapping an application error, never echo arbitrary messages.
            if set(data) != BASE_FIELDS:
                raise ValueError
            base = {**data, "ok": True, "error": ""}
            decode_reply(json.dumps(base).encode(), "configure", mac)
            error = data.get("error")
            if error in SAFE_ERRORS:
                raise PanelError(error)
            return decode_reply(raw, "configure", mac)
        if operation not in {"info", "binding"}:
            return decode_reply(raw, operation, mac)
        expected = INFO_FIELDS if operation == "info" else BINDING_FIELDS
        if set(data) != BASE_FIELDS | expected:
            raise ValueError
        decode_reply(json.dumps({k: data[k] for k in BASE_FIELDS}).encode(), "configure", mac)
        if data["mac"] != mac or data["model"] != MODEL:
            raise PanelError("identity_mismatch")
        if operation == "info":
            if (any(type(data[k]) is not bool for k in ("configured", "busy", "commissioning"))
                    or not isinstance(data["name"], str) or not 1 <= len(data["name"]) <= 63
                    or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", data["name"])
                    or not isinstance(data["friendly_name"], str) or len(data["friendly_name"]) > 128):
                raise ValueError
            version = data["version"]
            if (not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version)
                    or len(version) > 32 or tuple(map(int, version.split("."))) < MIN_COMMISSIONING_FIRMWARE):
                raise PanelError("commissioning_unsupported_firmware")
        else:
            if (data["phase"] != "committed" or data["expires_in"] != 0
                    or data["ha_origin"] != normalize_origin(data["ha_origin"])
                    or type(data["revision"]) is not int or not 1 <= data["revision"] <= 0xFFFFFFFF):
                raise ValueError
            validate_token(data["binding_id"])
            validate_selection(data["entity_ids"], data["weather_id"])
        return data
    except PanelError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise PanelError("invalid_response") from None


def _tls_context():
    # Only the bundled public device CA is trusted. The TLS peer hostname is
    # derived from the expected MAC, never from unauthenticated discovery data.
    context = ssl.create_default_context(cafile=str(Path(__file__).with_name("commissioning_ca.pem")))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.hostname_checks_common_name = False
    context.verify_mode = ssl.CERT_REQUIRED
    return context


async def verified_unbound_info(host, mac):
    """Public identity proof only; never start a session or accept a discovery claim."""
    client = CommissioningClient(host, mac)
    try:
        info = await client.connect()
        if info["configured"]:
            raise PanelError("commissioning_already_bound")
        if info["busy"]:
            raise PanelError("busy")
        if not info["commissioning"]:
            raise PanelError("commissioning_closed")
        return info
    finally:
        await client.close()


class CommissioningClient:
    """No Native API key; all operations use certificate-verified HTTPS."""

    transport = COMMISSIONING_TRANSPORT

    def __init__(self, host, mac, credential=None):
        self.host, self.mac = validate_host(host), normalize_mac(mac)
        self.credential = validate_token(credential) if credential is not None else None
        self.name = self.friendly_name = self.version = ""
        self._session = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def connect(self, *, update_probe=False, local_probe=False):
        try:
            if self._closed:
                raise PanelError("cancelled")
            context = await asyncio.to_thread(_tls_context)
            if self._closed:
                raise PanelError("cancelled")
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=context, limit=1),
                timeout=aiohttp.ClientTimeout(total=12), trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
            )
            if local_probe:
                info = await self.rpc("local_info", {})
                self.version = info["current_version"]
                return info
            if update_probe:
                try:
                    info = await self.rpc("update_info", {})
                except PanelError as error:
                    # Only an old operation table permits the legacy identity probe.
                    # Credential rejection, malformed replies and TLS errors never fall back.
                    if error.code != "update_unknown_operation":
                        raise
                else:
                    self.version = info["current_version"]
                    return info
            info = await self.rpc("info", {})
            self.name, self.friendly_name, self.version = info["name"], info["friendly_name"], info["version"]
            return info
        except BaseException as error:
            await self.close()
            if isinstance(error, (PanelError, asyncio.CancelledError)):
                raise
            raise PanelError("commissioning_unavailable") from None

    async def rpc(self, operation, payload):
        if (operation not in {"info", "configure", "reconfigure", "status", "authorize", "catalog", "commit", "cancel", "binding",
                              "update_info", "update_check", "update_install",
                              "local_info", "local_begin", "local_chunk", "local_finish", "local_abort"}
                or not isinstance(payload, dict)):
            raise PanelError("invalid_request")
        envelope = {"operation": operation, "payload": payload}
        if self.credential is not None and operation != "info":
            envelope["credential"] = validate_token(self.credential)
        try:
            body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode()
            if len(body) > 24 * 1024:
                raise PanelError("invalid_request")
            async with self._lock:
                if self._session is None or self._closed:
                    raise PanelError("commissioning_unavailable")
                async with self._session.post(
                    f"https://{self.host}:{COMMISSIONING_PORT}/v1/commission", data=body,
                    headers={"Content-Type": "application/json", "Accept": "application/json", "Accept-Encoding": "identity"},
                    server_hostname=f"ad35-{self.mac}.local", allow_redirects=False,
                ) as response:
                    if (response.status != 200 or response.headers.get("Content-Encoding", "identity") != "identity"
                            or response.content_type != "application/json"
                            or (response.content_length is not None and response.content_length > MAX_RESPONSE)):
                        raise PanelError("invalid_response")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(4096):
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE:
                            raise PanelError("invalid_response")
                    if operation in {"update_info", "update_check", "update_install"}:
                        from .update_protocol import decode_update_reply
                        return decode_update_reply(bytes(raw), self.mac)
                    if operation in {"local_info", "local_begin", "local_chunk", "local_finish", "local_abort"}:
                        from .local_protocol import decode_local_reply
                        return decode_local_reply(bytes(raw), self.mac)
                    return decode_commissioning(bytes(raw), operation, self.mac)
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError, ssl.SSLError):
            raise PanelError("commissioning_certificate_error") from None
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise PanelError("commissioning_unavailable") from None
        except (ValueError, TypeError):
            raise PanelError("invalid_request") from None

    async def close(self):
        self._closed = True
        if self._session is not None:
            await self._session.close()
            self._session = None
