"""Bounded local OTA replies and one authenticated connection for all chunks."""
from __future__ import annotations

import re

from .const import ALLOWLIST_FILE, COMMISSIONING_TRANSPORT, MODEL, PORT
from .protocol import (BASE_FIELDS, CAPABILITY, PanelClient, PanelError, build_device_config,
                       load_device_config, normalize_origin, strict_json)
from .update_protocol import version_tuple

MIN_LOCAL_FIRMWARE = (2, 5, 0)
MAX_SIZE = 0x7C0000
CHUNK_SIZE = 12288
OPERATIONS = {"local_info", "local_begin", "local_chunk", "local_finish", "local_abort"}
PHASES = {"idle", "preparing", "receiving", "verifying", "rebooting", "aborted", "error"}
ERRORS = {"invalid_payload", "invalid_credential", "unknown_operation", "not_ready", "busy",
          "request_mismatch", "invalid_session", "invalid_image", "wrong_device", "downgrade",
          "size_mismatch", "offset_mismatch", "hash_mismatch", "begin_failed", "write_failed",
          "verify_failed", "boot_failed", "incomplete", "commit_in_progress"}
LAST_ERRORS = ERRORS | {"aborted", "expired", "network_changed", "binding_changed", "interrupted"}
FIELDS = BASE_FIELDS | {"mac", "model", "current_version", "current_image_sha256", "boot_id",
                       "max_size", "chunk_size", "busy", "request_id", "target_version", "size",
                       "received", "sha256", "last_error", "running_partition", "target_partition"}


def decode_local_reply(raw, mac):
    try:
        if not isinstance(raw, bytes) or len(raw) > 4096:
            raise ValueError
        data = strict_json(raw)
        if (not isinstance(data, dict) or not BASE_FIELDS <= data.keys()
                or type(data["protocol"]) is not int or data["protocol"] != 1
                or type(data["ok"]) is not bool or data["phase"] not in PHASES
                or type(data["expires_in"]) is not int or not 0 <= data["expires_in"] <= 120):
            raise ValueError
        if not data["ok"]:
            if (data.keys() != BASE_FIELDS or data["error"] not in ERRORS
                    or data["phase"] != "idle" or data["expires_in"] != 0):
                raise ValueError
            raise PanelError("local_" + data["error"])
        if data.keys() != FIELDS or data["error"] != "":
            raise ValueError
        if data["mac"] != mac or data["model"] != MODEL:
            raise PanelError("identity_mismatch")
        version_tuple(data["current_version"])
        if data["target_version"]:
            version_tuple(data["target_version"])
        elif data["target_version"] != "":
            raise ValueError
        if (not isinstance(data["current_image_sha256"], str) or not CAPABILITY.fullmatch(data["current_image_sha256"])
                or not isinstance(data["boot_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", data["boot_id"])
                or type(data["busy"]) is not bool or data["last_error"] not in LAST_ERRORS | {""}):
            raise ValueError
        for key, maximum in (("max_size", MAX_SIZE), ("chunk_size", CHUNK_SIZE), ("size", MAX_SIZE),
                             ("received", MAX_SIZE), ("running_partition", 0x1000000), ("target_partition", 0x1000000)):
            if type(data[key]) is not int or not 0 <= data[key] <= maximum:
                raise ValueError
        if (data["max_size"] < 384 or not data["chunk_size"] or data["received"] > data["size"]
                or data["size"] > data["max_size"] or not data["running_partition"]
                or data["running_partition"] % 4096 or data["target_partition"] % 4096):
            raise ValueError
        for key in ("request_id", "sha256"):
            if not isinstance(data[key], str) or (data[key] and not CAPABILITY.fullmatch(data[key])):
                raise ValueError
        if data["phase"] in {"preparing", "receiving", "verifying", "rebooting"}:
            if (not data["busy"] or not data["request_id"] or not data["sha256"] or not data["target_version"]
                    or not data["target_partition"] or data["target_partition"] == data["running_partition"]
                    or data["size"] < 384):
                raise ValueError
        return data
    except PanelError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise PanelError("invalid_response") from None


class LocalTransport:
    """Reuse a connection; only an explicit read may reconnect after an unknown write."""
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.client = None
        self.closed = False

    async def connect(self):
        from .commissioning import CommissioningClient, validate_token
        if self.closed:
            raise PanelError("cancelled")
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
        self.client = client
        try:
            if self.closed:
                raise PanelError("cancelled")
            info = await client.connect(local_probe=True)
            if self.closed:
                raise PanelError("cancelled")
            if version_tuple(info["current_version"]) < MIN_LOCAL_FIRMWARE:
                raise PanelError("local_requires_firmware")
            return info
        except BaseException as error:
            await self.disconnect()
            if isinstance(error, PanelError) and error.code == "local_unknown_operation":
                raise PanelError("local_requires_firmware") from None
            raise

    async def rpc(self, operation, payload):
        if self.closed or operation not in OPERATIONS:
            raise PanelError("cancelled")
        if self.client is None:
            # No implicit reconnect ever causes a write to be repeated.
            if operation != "local_info":
                raise PanelError("cannot_connect")
            return await self.connect()
        return await self.client.rpc(operation, payload)

    async def disconnect(self):
        client, self.client = self.client, None
        if client is not None:
            await client.close()

    async def close(self):
        self.closed = True
        await self.disconnect()
