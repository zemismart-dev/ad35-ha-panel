"""Strict firmware-update replies and short-lived, authenticated transports."""
from __future__ import annotations

import re

from .const import ALLOWLIST_FILE, COMMISSIONING_TRANSPORT, MODEL, PORT
from .protocol import (BASE_FIELDS, CAPABILITY, PanelClient, PanelError,
                       build_device_config, load_device_config, normalize_origin, strict_json)

MIN_UPDATE_FIRMWARE = (2, 4, 0)
OPERATIONS = {"update_info", "update_check", "update_install"}
PHASES = {"idle", "check_queued", "checking", "install_queued", "installing", "succeeded", "external_ota"}
FIELDS = BASE_FIELDS | {"mac", "model", "current_version", "latest_version", "summary", "status",
                       "configured", "busy", "can_install", "candidate_id", "request_id", "last_error"}
ERRORS = {"invalid_payload", "invalid_credential", "not_configured", "not_ready", "busy",
          "no_candidate", "candidate_mismatch", "request_mismatch", "check_failed",
          "manifest_invalid", "download_auth_failed", "update_failed", "interrupted", "unknown_operation"}
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


def version_tuple(value):
    if not isinstance(value, str) or len(value) > 32 or not VERSION.fullmatch(value):
        raise PanelError("invalid_response")
    parts = tuple(map(int, value.split(".")))
    if any(part > 0xFFFFFFFF for part in parts):
        raise PanelError("invalid_response")
    return parts


def decode_update_reply(raw, mac):
    """Unknown fields/errors never become exception text or unchecked UI data."""
    try:
        if not isinstance(raw, bytes) or len(raw) > 4096:
            raise ValueError
        data = strict_json(raw)
        if (not isinstance(data, dict) or not BASE_FIELDS <= data.keys()
                or type(data["protocol"]) is not int or data["protocol"] != 1
                or type(data["ok"]) is not bool or data["phase"] not in PHASES
                or type(data["expires_in"]) is not int or not 0 <= data["expires_in"] <= 600
                or not isinstance(data["error"], str)):
            raise ValueError
        if not data["ok"]:
            if data.keys() != BASE_FIELDS or data["error"] not in ERRORS:
                raise ValueError
            raise PanelError("update_" + data["error"])
        if data.keys() != FIELDS or data["error"]:
            raise ValueError
        if data["mac"] != mac or data["model"] != MODEL:
            raise PanelError("identity_mismatch")
        version_tuple(data["current_version"])
        if data["latest_version"]:
            version_tuple(data["latest_version"])
        elif data["latest_version"] != "":
            raise ValueError
        for key, limit in (("summary", 240), ("status", 128)):
            value = data[key]
            if not isinstance(value, str) or len(value.encode()) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
                raise ValueError
        for key in ("configured", "busy", "can_install"):
            if type(data[key]) is not bool:
                raise ValueError
        for key in ("candidate_id", "request_id"):
            if not isinstance(data[key], str) or (data[key] and not CAPABILITY.fullmatch(data[key])):
                raise ValueError
        if data["candidate_id"] and data["expires_in"] == 0:
            raise ValueError
        if data["last_error"] not in ERRORS | {""}:
            raise ValueError
        if data["can_install"] and (not data["configured"] or data["busy"] or not data["candidate_id"]
                                    or not data["latest_version"] or data["expires_in"] == 0):
            raise ValueError
        return data
    except PanelError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise PanelError("invalid_response") from None


def unsupported_status(version, mac):
    return {"protocol": 1, "ok": True, "phase": "idle", "error": "", "expires_in": 0,
            "mac": mac, "model": MODEL, "current_version": version, "latest_version": "",
            "summary": "", "status": "", "configured": False, "busy": False, "can_install": False,
            "candidate_id": "", "request_id": "", "last_error": "requires_firmware"}


class UpdateTransport:
    """Each operation validates device identity; no retained Native maintenance key export."""

    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self._client = None
        self._closed = False

    async def rpc(self, operation, payload):
        if self._closed or operation not in OPERATIONS:
            raise PanelError("cancelled")
        from .commissioning import CommissioningClient, validate_token
        data, mac = self.entry.data, self.entry.unique_id
        https = data.get("transport") == COMMISSIONING_TRANSPORT
        if https:
            client = CommissioningClient(data["host"], mac, validate_token(data.get("configuration_token")))
        else:
            if "noise_psk" in data:
                config = build_device_config(data["noise_psk"], data["ha_origin"], port=data.get("port", PORT))
            elif data.get("psk_reference") == ALLOWLIST_FILE:
                config = await self.hass.async_add_executor_job(load_device_config, self.hass.config.path(ALLOWLIST_FILE), mac)
                if config.ha_origin != normalize_origin(data["ha_origin"]):
                    raise PanelError("origin_mismatch")
            else:
                raise PanelError("invalid_noise_psk")
            client = PanelClient(data.get("host") or config.host, config, mac)
        self._client = client
        try:
            if self._closed:
                raise PanelError("cancelled")
            info = await client.connect(update_probe=True) if https else await client.connect()
            if self._closed:
                raise PanelError("cancelled")
            if version_tuple(client.version) < MIN_UPDATE_FIRMWARE:
                if operation != "update_info":
                    raise PanelError("update_requires_firmware")
                return unsupported_status(client.version, mac)
            if https and operation == "update_info" and "current_version" in info:
                return info
            return await client.rpc(operation, payload)
        finally:
            await client.close()
            self._client = None

    async def close(self):
        self._closed = True
        if self._client is not None:
            await self._client.close()
