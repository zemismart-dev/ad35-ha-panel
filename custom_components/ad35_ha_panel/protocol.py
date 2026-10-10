"""Bounded Native API client. No HA service registry, tokens or logging."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import ipaddress
import json
import os
import re
import secrets
import stat
import time
from urllib.parse import urlsplit

from .const import (
    COMMISSIONING_TRANSPORT, MIN_EMPTY_SELECTION_FIRMWARE, SELECTABLE_DOMAINS,
    MIN_FIRMWARE, MODEL, NATIVE_MODEL, PORT, PROJECT, SERVICE,
)

BASE_FIELDS = {"protocol", "ok", "phase", "error", "expires_in"}
PHASES = {
    "idle", "pending_local", "approved", "authorizing", "catalog_ready",
    "saving", "committed", "rejected", "expired", "cancelled", "error",
}
TERMINAL = {"rejected", "expired", "cancelled", "error"}
CAPABILITY = re.compile(r"[0-9a-f]{64}\Z")
ENTITY = re.compile(r"[a-z_]+\.[a-z0-9_]+\Z")


class PanelError(Exception):
    """Only fixed, non-secret error identifiers cross component boundaries."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def normalize_mac(value: str) -> str:
    if not isinstance(value, str):
        raise PanelError("identity_mismatch")
    value = value.lower().replace(":", "").replace("-", "")
    if not re.fullmatch(r"[0-9a-f]{12}", value):
        raise PanelError("identity_mismatch")
    return value


def normalize_origin(value: str) -> str:
    """Match firmware: RFC1918 IPv4 HTTP or certificate-verified HTTPS."""
    try:
        if not isinstance(value, str) or len(value) > 256 or any(c.isspace() for c in value):
            raise ValueError
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        if (parts.scheme not in {"http", "https"} or not host or parts.username is not None
                or parts.password is not None or parts.path not in {"", "/"}
                or parts.query or parts.fragment or ":" in host):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if parts.scheme == "http":
            addr = ipaddress.IPv4Address(host)
            if not any(addr in ipaddress.IPv4Network(n) for n in
                       ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
                raise ValueError
        elif not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", host):
            raise ValueError
        default = 80 if parts.scheme == "http" else 443
        return f"{parts.scheme}://{host.lower()}" + (f":{port}" if port and port != default else "")
    except (ValueError, TypeError):
        raise PanelError("invalid_origin") from None


def validate_host(host: str) -> str:
    if (not isinstance(host, str) or len(host) > 253 or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)):
        raise PanelError("invalid_host")
    return host


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def strict_json(value: str | bytes):
    return json.loads(value, object_pairs_hook=_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


@dataclass(repr=False, frozen=True)
class DeviceConfig:
    noise_psk: str = field(repr=False)
    ha_origin: str
    host: str | None = None
    port: int = PORT


def validate_noise_psk(value: str) -> str:
    """Require exactly one canonical Base64 encoding of a 32-byte Noise key."""
    try:
        if not isinstance(value, str) or len(value) != 44:
            raise ValueError
        decoded = base64.b64decode(value, validate=True)
        if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError
    except (ValueError, TypeError):
        raise PanelError("invalid_noise_psk") from None
    return value


def build_device_config(noise_psk: str, ha_origin: str, host: str | None = None,
                        port: int = PORT) -> DeviceConfig:
    """Validate form or entry data before constructing an encrypted client."""
    key = validate_noise_psk(noise_psk)
    origin = normalize_origin(ha_origin)
    if type(port) is not int or port != PORT:
        raise PanelError("invalid_port")
    return DeviceConfig(key, origin, validate_host(host) if host is not None else None, port)


def load_device_config(path: str, mac: str) -> DeviceConfig:
    """Read a small owner-only regular file without following a symlink."""
    mac = normalize_mac(mac)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid() or info.st_size > 16384):
                raise PanelError("unsafe_allowlist")
            root = strict_json(file.read(16385))
        if not isinstance(root, dict):
            raise ValueError
        if mac not in root:
            raise PanelError("not_allowlisted")
        data = root[mac]
        if not isinstance(data, dict) or not {"noise_psk", "ha_origin"} <= data.keys():
            raise ValueError
        if data.keys() - {"noise_psk", "ha_origin", "host", "port"}:
            raise ValueError
        try:
            return build_device_config(data["noise_psk"], data["ha_origin"],
                                       data.get("host") or None, data.get("port", PORT))
        except PanelError as error:
            if error.code in {"invalid_noise_psk", "invalid_port"}:
                raise PanelError("unsafe_allowlist") from None
            raise
    except FileNotFoundError:
        raise PanelError("not_allowlisted") from None
    except PanelError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise PanelError("unsafe_allowlist") from None


def decode_reply(raw: bytes, operation: str, expected_mac: str | None = None) -> dict:
    if not isinstance(raw, bytes) or len(raw) > 16 * 1024:
        raise PanelError("invalid_response")
    try:
        reply = strict_json(raw)
        extras = {"info": {"mac", "model", "configured", "busy"},
                  "status": {"session"}, "catalog": {"total", "offset", "items"}}.get(operation, set())
        if (not isinstance(reply, dict) or not BASE_FIELDS <= reply.keys()
                or reply.keys() - BASE_FIELDS - extras
                or type(reply["protocol"]) is not int or reply["protocol"] != 1
                or type(reply["ok"]) is not bool or reply["phase"] not in PHASES
                or not isinstance(reply["error"], str) or len(reply["error"]) > 64
                or type(reply["expires_in"]) is not int or not 0 <= reply["expires_in"] <= 600):
            raise ValueError
        if operation == "info":
            if (not extras <= reply.keys() or not isinstance(reply["mac"], str)
                    or not re.fullmatch(r"[0-9a-f]{12}", reply["mac"])
                    or (expected_mac is not None and reply["mac"] != normalize_mac(expected_mac))
                    or reply["model"] != MODEL or type(reply["configured"]) is not bool
                    or type(reply["busy"]) is not bool):
                raise PanelError("identity_mismatch")
        if not reply["ok"]:
            safe_errors = {"already_bound", "busy", "expired", "rejected", "cancelled",
                           "commit_in_progress", "already_authorized", "not_bound",
                           "origin_mismatch", "binding_changed"}
            raise PanelError(reply["error"] if reply["error"] in safe_errors else "device_rejected")
        if reply["error"]:
            raise ValueError
        if "session" in reply and (not isinstance(reply["session"], str)
                                    or not CAPABILITY.fullmatch(reply["session"])):
            raise ValueError
        if operation == "catalog":
            if (not extras <= reply.keys() or type(reply["total"]) is not int
                    or not 0 <= reply["total"] <= 256 or type(reply["offset"]) is not int
                    or not 0 <= reply["offset"] <= reply["total"]
                    or not isinstance(reply["items"], list) or len(reply["items"]) > 16):
                raise ValueError
            for item in reply["items"]:
                if (not isinstance(item, dict) or set(item) != {"entity_id", "name", "domain", "available"}
                        or not isinstance(item["entity_id"], str) or len(item["entity_id"]) > 255
                        or not ENTITY.fullmatch(item["entity_id"])
                        or item["domain"] != item["entity_id"].split(".")[0]
                        or item["domain"] not in SELECTABLE_DOMAINS | {"weather"}
                        or not isinstance(item["name"], str) or len(item["name"]) > 512
                        or type(item["available"]) is not bool):
                    raise ValueError
        return reply
    except PanelError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        raise PanelError("invalid_response") from None


class PanelClient:
    """One encrypted, identity-checked direct API connection per flow."""

    def __init__(self, host: str, config: DeviceConfig, mac: str):
        self.mac = normalize_mac(mac)
        config = build_device_config(config.noise_psk, config.ha_origin, config.host, config.port)
        from aioesphomeapi import APIClient
        self._client = APIClient(validate_host(host), config.port, noise_psk=config.noise_psk,
                                 expected_mac=self.mac,
                                 client_info="AD35 HA pairing")
        # Payloads include short-lived credentials; never enable SDK packet logging here.
        self._client.set_debug(False)
        self._service = None
        self.version = ""
        self.name = ""
        self.friendly_name = ""

    @staticmethod
    def _label(value, fallback: str) -> str:
        """Use bounded printable device labels; older SDKs can omit friendly_name."""
        if not isinstance(value, str) or not value or len(value) > 128 or not value.isprintable():
            return fallback
        return value.strip() or fallback

    async def connect(self, *, local_probe=False) -> dict:
        try:
            async with asyncio.timeout(15):
                await self._client.connect(login=True, log_errors=False)
                info = await self._client.device_info()
                if (normalize_mac(getattr(info, "mac_address", None)) != self.mac
                        or getattr(info, "project_name", None) != PROJECT
                        or getattr(info, "model", None) != NATIVE_MODEL):
                    raise PanelError("identity_mismatch")
                self.version = getattr(info, "project_version", "")
                version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", self.version) if isinstance(self.version, str) and len(self.version) <= 32 else None
                if version is None or tuple(map(int, version.groups())) < MIN_FIRMWARE:
                    raise PanelError("unsupported_firmware")
                _, services = await self._client.list_entities_services()
                matches = [service for service in services if service.name == SERVICE]
                from aioesphomeapi.model import SupportsResponseType, UserServiceArgType
                if len(matches) != 1:
                    raise PanelError("unsupported_firmware")
                service = matches[0]
                if (service.supports_response not in {SupportsResponseType.OPTIONAL, SupportsResponseType.ONLY}
                        or len(service.args) != 2
                        or {arg.name: arg.type for arg in service.args} != {
                            "operation": UserServiceArgType.STRING, "payload": UserServiceArgType.STRING}):
                    raise PanelError("unsupported_firmware")
                self._service = service
                result = await self.rpc("local_info" if local_probe else "info", {})
                self.name = self._label(getattr(info, "name", None), f"AD35 {self.mac[-6:]}")
                self.friendly_name = self._label(getattr(info, "friendly_name", None), self.name)
                return result
        except PanelError:
            raise
        except Exception:
            raise PanelError("cannot_connect") from None

    async def rpc(self, operation: str, payload: dict) -> dict:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode()) > 24 * 1024 or self._service is None:
            raise PanelError("invalid_request")
        try:
            response = await self._client.execute_service(
                self._service, {"operation": operation, "payload": encoded},
                return_response=True, timeout=5.0,
            )
        except Exception:
            raise PanelError("cannot_connect") from None
        if response is None or not response.success:
            raise PanelError("native_api_failed")
        if operation in {"update_info", "update_check", "update_install"}:
            from .update_protocol import decode_update_reply
            return decode_update_reply(response.response_data, self.mac)
        if operation in {"local_info", "local_begin", "local_chunk", "local_finish", "local_abort"}:
            from .local_protocol import decode_local_reply
            return decode_local_reply(response.response_data, self.mac)
        return decode_reply(response.response_data, operation, expected_mac=self.mac)

    async def close(self):
        try:
            await self._client.disconnect()
        except Exception:
            pass


class PairingSession:
    """One-use candidate transaction, held only in config-flow memory."""

    def __init__(self, client, origin: str, client_id: str, *, poll_interval=1.0,
                 operation="request"):
        if operation not in {"request", "configure", "reconfigure"}:
            raise PanelError("invalid_request")
        self.client = client
        self.operation = operation
        self.origin = origin
        self.client_id = client_id
        self.request_id = secrets.token_hex(32)
        self.session = ""
        self.phase = "idle"
        self.started = False
        self.authorization_sent = False
        self.commit_sent = False
        self.catalog = []
        self.deadline = 0.0
        self.poll_interval = poll_interval

    def _payload(self):
        return {"request_id": self.request_id, **({"session": self.session} if self.session else {})}

    def _accept(self, reply):
        self.phase = reply["phase"]
        if self.phase in TERMINAL:
            raise PanelError(self.phase if self.phase != "error" else "device_rejected")
        return reply

    async def wait_local(self):
        if not self.started:
            self.started = True
            self.deadline = time.monotonic() + 125
            self._accept(await self.client.rpc(self.operation, {
                "request_id": self.request_id, "ha_origin": self.origin, "client_id": self.client_id,
            }))
        reply = await self.wait_for({"approved"}, {"pending_local", "approved"})
        session = reply.get("session", "")
        if not CAPABILITY.fullmatch(session):
            raise PanelError("invalid_response")
        self.session = session
        self.deadline = time.monotonic() + min(reply["expires_in"], 600)

    async def wait_for(self, phases: set[str], allowed: set[str]):
        while time.monotonic() < self.deadline:
            reply = self._accept(await self.client.rpc("status", self._payload()))
            if self.phase not in allowed:
                raise PanelError("invalid_phase")
            if self.phase in phases:
                return reply
            await asyncio.sleep(self.poll_interval)
        raise PanelError("expired")

    async def authorize(self, code: str):
        if self.authorization_sent or self.phase != "approved" or not self.session:
            raise PanelError("invalid_phase")
        if not isinstance(code, str) or not 0 < len(code.encode()) <= 4096:
            raise PanelError("invalid_authorization")
        if time.monotonic() >= self.deadline:
            raise PanelError("expired")
        # Mark before I/O: even an ambiguous network failure must never resend a code.
        self.authorization_sent = True
        self._accept(await self.client.rpc("authorize", {**self._payload(), "code": code}))
        await self.wait_for({"catalog_ready"}, {"authorizing", "catalog_ready"})
        catalog, total = [], None
        while total is None or len(catalog) < total:
            reply = self._accept(await self.client.rpc("catalog", {**self._payload(), "offset": len(catalog)}))
            if reply["phase"] != "catalog_ready" or reply["offset"] != len(catalog):
                raise PanelError("invalid_response")
            if total is not None and reply["total"] != total:
                raise PanelError("invalid_response")
            total = reply["total"]
            items = reply["items"]
            if len(items) != min(16, total - len(catalog)):
                raise PanelError("invalid_response")
            catalog.extend(items)
        if len({item["entity_id"] for item in catalog}) != len(catalog):
            raise PanelError("invalid_response")
        self.catalog = catalog

    def validate_selection(self, entity_ids, weather_id):
        allowed = {item["entity_id"] for item in self.catalog if item["domain"] in SELECTABLE_DOMAINS}
        weather = {item["entity_id"] for item in self.catalog if item["domain"] == "weather"}
        if (not isinstance(entity_ids, list) or len(entity_ids) > 64
                or any(not isinstance(item, str) for item in entity_ids)
                or len(set(entity_ids)) != len(entity_ids) or not set(entity_ids) <= allowed
                or not isinstance(weather_id, str) or (weather_id and weather_id not in weather)):
            raise PanelError("invalid_selection")
        if (not entity_ids and not weather_id
                and getattr(self.client, "transport", None) == COMMISSIONING_TRANSPORT):
            # Native commits already support zero items. Scoped HTTPS acquired
            # that capability in 2.7.2; use this connection's verified version,
            # never a discovery claim or stale HA entry firmware value.
            value = self.client.version
            version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value) if isinstance(value, str) and len(value) <= 32 else None
            if version is None or tuple(map(int, version.groups())) < MIN_EMPTY_SELECTION_FIRMWARE:
                raise PanelError("empty_selection_requires_firmware")

    async def commit(self, entity_ids, weather_id, *, configuration_token=None):
        if self.phase != "catalog_ready" or self.commit_sent or time.monotonic() >= self.deadline:
            raise PanelError("invalid_phase")
        self.validate_selection(entity_ids, weather_id)
        extra = {}
        if configuration_token is not None:
            if not isinstance(configuration_token, str) or not CAPABILITY.fullmatch(configuration_token):
                raise PanelError("invalid_credential")
            extra["configuration_token"] = configuration_token
        self.commit_sent = True
        self._accept(await self.client.rpc("commit", {
            **self._payload(), "entity_ids": entity_ids, "weather_id": weather_id, **extra,
        }))
        await self.wait_for({"committed"}, {"saving", "committed"})

    async def close(self):
        try:
            if self.started and not self.commit_sent and self.phase not in TERMINAL | {"committed"}:
                # The screen may approve between our last poll and flow cancellation.
                # Recover the capability before cancelling; never relax the device gate.
                if not self.session:
                    current = await self.client.rpc("status", self._payload())
                    if current["phase"] == "approved":
                        capability = current.get("session", "")
                        if not CAPABILITY.fullmatch(capability):
                            raise PanelError("invalid_response")
                        self.session = capability
                await self.client.rpc("cancel", self._payload())
        except (PanelError, asyncio.CancelledError):
            pass
        finally:
            self.request_id = self.session = ""
            self.catalog = []
            await self.client.close()
