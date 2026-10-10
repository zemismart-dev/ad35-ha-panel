"""One-use temporary-code bootstrap using Espressif's official Security2 client."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import re
import time

import aiohttp

from .const import BOOTSTRAP_PORT, MIN_PIN_FIRMWARE, MODEL
from .protocol import PanelError, normalize_mac, strict_json, validate_host, validate_noise_psk
from .vendor.esp_prov import proto
from .vendor.esp_prov.security2 import Security2


@dataclass(frozen=True, repr=False)
class BootstrapCredentials:
    mac: str
    noise_psk: str = field(repr=False)
    host: str
    firmware: str


def pin_firmware_supported(version):
    return (isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version) is not None
            and tuple(map(int, version.split("."))) >= MIN_PIN_FIRMWARE)


def validate_pin(pin):
    if not isinstance(pin, str) or re.fullmatch(r"[0-9]{8}", pin) is None:
        raise PanelError("invalid_pin")
    return pin


def _json(raw, fields):
    try:
        data = strict_json(raw)
        if not isinstance(data, dict) or set(data) != fields:
            raise ValueError
        return data
    except (ValueError, TypeError, UnicodeError):
        raise PanelError("bootstrap_invalid_response") from None


def _session_response(raw, stage):
    """Bound and validate the envelope before invoking official cryptography."""
    try:
        message = proto.session_pb2.SessionData()
        message.ParseFromString(raw)
        original = message.SerializeToString(deterministic=True)
        message.DiscardUnknownFields()
        if original != message.SerializeToString(deterministic=True):
            raise ValueError
        expected = proto.sec2_pb2.S2Session_Response0 if stage == 0 else proto.sec2_pb2.S2Session_Response1
        name = "sr0" if stage == 0 else "sr1"
        if (message.sec_ver != 2 or message.WhichOneof("proto") != "sec2"
                or message.sec2.msg != expected or message.sec2.WhichOneof("payload") != name):
            raise ValueError
        payload = getattr(message.sec2, name)
        if payload.status != 0:
            raise PanelError("bootstrap_auth_failed")
        if stage == 0:
            if not 1 <= len(payload.device_pubkey) <= 384 or len(payload.device_salt) != 16:
                raise ValueError
        elif len(payload.device_proof) != 64 or len(payload.device_nonce) != 12:
            raise ValueError
        return raw.decode("latin-1")
    except PanelError:
        raise
    except Exception:
        raise PanelError("bootstrap_invalid_response") from None


class BootstrapClient:
    """Own exactly one HTTP connection; no cookies, redirects, or reconnects."""

    def __init__(self, host, mac):
        self.host = validate_host(host)
        self.mac = normalize_mac(mac)
        self._http = None
        self._security = None
        self._metadata = None
        self._deadline = 0.0
        self._requests = 0
        self._closed = False
        self._claim_started = False

    async def _new_connection(self, session, context, params):
        if self._requests != 1 or self._closed:
            raise PanelError("bootstrap_connection_lost")

    async def _post(self, endpoint, body, *, limit=1024, json_body=False):
        if self._closed or self._http is None:
            raise PanelError("bootstrap_connection_lost")
        if self._deadline and time.monotonic() >= self._deadline:
            raise PanelError("bootstrap_expired")
        if len(body) > 1024:
            raise PanelError("bootstrap_invalid_response")
        self._requests += 1
        async with self._http.post(
            f"http://{self.host}:{BOOTSTRAP_PORT}/{endpoint}", data=body,
            headers={"Content-Type": "application/json" if json_body else "application/octet-stream"},
            allow_redirects=False,
        ) as response:
            if response.status in {301, 302, 303, 307, 308}:
                raise PanelError("bootstrap_invalid_response")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise PanelError("bootstrap_invalid_response")
            if response.content_length is not None and response.content_length > limit:
                raise PanelError("bootstrap_invalid_response")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(1024):
                raw.extend(chunk)
                if len(raw) > limit:
                    raise PanelError("bootstrap_invalid_response")
            if response.status != 200:
                # Neither a remote error string nor an HTTP exception can echo
                # a submitted code, recovered key, or arbitrary response body.
                safe = {
                    "busy": "bootstrap_busy", "already_bound": "bootstrap_already_bound",
                    "expired": "bootstrap_expired", "locked": "bootstrap_locked",
                    "rate_limited": "bootstrap_locked", "invalid_pin": "bootstrap_auth_failed",
                    "auth_failed": "bootstrap_auth_failed", "unavailable": "bootstrap_unavailable",
                    "not_allowed": "bootstrap_already_bound", "session_lost": "bootstrap_connection_lost",
                    "security_failed": "bootstrap_auth_failed", "internal_error": "bootstrap_failed",
                    "invalid_request": "bootstrap_invalid_response",
                }
                error = _json(bytes(raw), {"error"})
                code = error.get("error")
                raise PanelError(safe.get(code, "bootstrap_failed") if isinstance(code, str) else "bootstrap_failed")
            return bytes(raw)

    async def start(self):
        if self._http is not None or self._closed:
            raise PanelError("bootstrap_connection_lost")
        trace = aiohttp.TraceConfig()
        trace.on_connection_create_start.append(self._new_connection)
        self._http = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=1, limit_per_host=1, keepalive_timeout=130),
            cookie_jar=aiohttp.DummyCookieJar(), trust_env=False, auto_decompress=False,
            headers={"Accept-Encoding": "identity"},
            timeout=aiohttp.ClientTimeout(total=10), trace_configs=[trace],
            max_line_size=2048, max_field_size=1024,
        )
        try:
            started_at = time.monotonic()
            raw = await self._post("ad35-start", json.dumps(
                {"protocol": 1, "mac": self.mac}, separators=(",", ":"),
            ).encode(), json_body=True)
            if self._closed:
                raise PanelError("bootstrap_connection_lost")
            data = _json(raw, {"protocol", "mac", "model", "firmware", "session",
                               "expires_in", "security", "sec_patch"})
            if (type(data["protocol"]) is not int or data["protocol"] != 1
                    or data["mac"] != self.mac or data["model"] != MODEL
                    or type(data["security"]) is not int or data["security"] != 2
                    or type(data["sec_patch"]) is not int or data["sec_patch"] != 1
                    or not isinstance(data["session"], str)
                    or re.fullmatch(r"[0-9a-f]{32}", data["session"]) is None
                    or type(data["expires_in"]) is not int or not 1 <= data["expires_in"] <= 120):
                raise PanelError("bootstrap_invalid_response")
            if not pin_firmware_supported(data["firmware"]):
                raise PanelError("bootstrap_unsupported_firmware")
            self._metadata = data
            self._deadline = started_at + data["expires_in"]
            return dict(data)
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as error:
            await self.close()
            if isinstance(error, PanelError):
                raise
            raise PanelError("bootstrap_unavailable") from None

    async def claim(self, pin):
        validate_pin(pin)
        if self._claim_started or self._metadata is None or self._closed:
            raise PanelError("bootstrap_connection_lost")
        self._claim_started = True
        try:
            # CPU-bound modular exponentiation runs off HA's event loop. The
            # upstream verbose flag is always false; no secret logging hooks.
            security = Security2(1, self.mac, pin, False)
            self._security = security
            request0 = await asyncio.to_thread(security.setup0_request)
            raw0 = await self._post("prov-session", request0.encode("latin-1"))
            response0 = _session_response(raw0, 0)
            await asyncio.to_thread(security.setup0_response, response0)
            request1 = security.setup1_request()
            raw1 = await self._post("prov-session", request1.encode("latin-1"))
            response1 = _session_response(raw1, 1)
            await asyncio.to_thread(security.setup1_response, response1)
            if not security.srp6a_ctx.authenticated():
                raise PanelError("bootstrap_auth_failed")
            request = json.dumps({"protocol": 1, "mac": self.mac,
                                  "session": self._metadata["session"]}, separators=(",", ":")).encode()
            raw_claim = await self._post("ad35-claim", security.encrypt_data(request))
            payload = security.decrypt_data(raw_claim)
            if len(payload) > 768:
                raise PanelError("bootstrap_invalid_response")
            data = _json(payload, {"protocol", "mac", "model", "firmware", "session", "noise_psk"})
            if (type(data["protocol"]) is not int or data["protocol"] != 1
                    or any(data[key] != self._metadata[key] for key in ("mac", "model", "firmware", "session"))):
                raise PanelError("bootstrap_invalid_response")
            key = validate_noise_psk(data["noise_psk"])
            return BootstrapCredentials(self.mac, key, self.host, data["firmware"])
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise PanelError("bootstrap_connection_lost") from None
        except Exception as error:
            if isinstance(error, PanelError):
                raise
            # Includes a mismatching server proof or AES-GCM tag. Never allow a
            # failed proof to be mistaken for successful credential recovery.
            raise PanelError("bootstrap_auth_failed") from None
        finally:
            await self.close()

    async def close(self):
        self._closed = True
        self._security = None
        self._metadata = None
        self._deadline = 0.0
        if self._http is not None:
            await self._http.close()
