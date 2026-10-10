"""Private, durable commissioning recovery journal; never contains OAuth codes.

Core Store.async_save logs and suppresses write failures. This journal instead
requires a successful fsync/replace/fsync before the device may receive commit.
Successful records remain as recovery credentials because HA config-entry writes
are delayed independently; deleting the journal on create_entry is not durable.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import tempfile

from .commissioning import validate_selection, validate_token
from .const import COMMISSIONING_PORT, COMMISSIONING_TRANSPORT, DOMAIN, MODEL
from .protocol import PanelError, normalize_mac, normalize_origin, strict_json, validate_host

MAX_BYTES = 2 * 1024 * 1024
ENTRY_FIELDS = {"mac", "host", "port", "ha_origin", "name", "friendly_name", "model", "firmware",
                "transport", "configuration_token", "binding_id"}
RECORD_FIELDS = {"entry_data", "entity_ids", "weather_id", "expected_revision"}


def validate_record(record):
    try:
        if not isinstance(record, dict) or set(record) != RECORD_FIELDS:
            raise ValueError
        data = record["entry_data"]
        if (not isinstance(data, dict) or set(data) != ENTRY_FIELDS
                or data["mac"] != normalize_mac(data["mac"])
                or data["ha_origin"] != normalize_origin(data["ha_origin"])
                or data["host"] != validate_host(data["host"])
                or data["model"] != MODEL or data["port"] != COMMISSIONING_PORT
                or type(data["port"]) is not int or data["transport"] != COMMISSIONING_TRANSPORT
                or any(not isinstance(data[k], str) or len(data[k]) > 128 for k in ("name", "friendly_name", "firmware"))
                or type(record["expected_revision"]) is not int
                or not 0 <= record["expected_revision"] <= 0xFFFFFFFF):
            raise ValueError
        validate_token(data["configuration_token"])
        validate_token(data["binding_id"])
        validate_selection(record["entity_ids"], record["weather_id"])
        return record
    except (ValueError, KeyError, TypeError, PanelError):
        raise PanelError("recovery_storage_error") from None


def match_binding(record, binding):
    validate_record(record)
    data = record["entry_data"]
    if (binding["mac"] != data["mac"] or binding["ha_origin"] != data["ha_origin"]
            or binding["binding_id"] != data["binding_id"]
            or binding["entity_ids"] != record["entity_ids"] or binding["weather_id"] != record["weather_id"]
            or (record["expected_revision"] and binding["revision"] != record["expected_revision"])):
        raise PanelError("recovery_mismatch")


def _read(path):
    try:
        directory = Path(path).parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid():
            raise ValueError
    except FileNotFoundError:
        return {}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as file:
        meta = os.fstat(file.fileno())
        if (not stat.S_ISREG(meta.st_mode) or stat.S_IMODE(meta.st_mode) != 0o600
                or meta.st_uid != os.geteuid() or meta.st_size > MAX_BYTES):
            raise ValueError
        raw = file.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError
        data = strict_json(raw)
    if not isinstance(data, dict) or len(data) > 64:
        raise ValueError
    for mac, record in data.items():
        validate_record(record)
        if mac != record["entry_data"]["mac"]:
            raise ValueError
    return data


def _write(path, records):
    folder = Path(path).parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = folder.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.geteuid():
        raise ValueError
    raw = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode()
    if len(records) > 64 or len(raw) > MAX_BYTES:
        raise ValueError
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".ad35-pending-", dir=folder)
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            os.unlink(temporary)


class PendingJournal:
    def __init__(self, hass):
        self.hass = hass
        self.path = hass.config.path(".storage/ad35_ha_panel.pending")
        self._lock = asyncio.Lock()

    async def get(self, mac):
        mac = normalize_mac(mac)
        async with self._lock:
            try:
                records = await self.hass.async_add_executor_job(_read, self.path)
                return deepcopy(records.get(mac))
            except Exception:
                raise PanelError("recovery_storage_error") from None

    async def put(self, record):
        record = deepcopy(validate_record(record))
        async with self._lock:
            try:
                records = await self.hass.async_add_executor_job(_read, self.path)
                records[record["entry_data"]["mac"]] = record
                # Shield the filesystem mutation so cancellation cannot release
                # this per-HA lock while a late worker still replaces the file.
                task = asyncio.ensure_future(self.hass.async_add_executor_job(_write, self.path, records))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            except asyncio.CancelledError:
                raise
            except Exception:
                raise PanelError("recovery_storage_error") from None


def journal(hass):
    data = hass.data.setdefault(DOMAIN, {})
    if "pending_journal" not in data:
        data["pending_journal"] = PendingJournal(hass)
    return data["pending_journal"]
