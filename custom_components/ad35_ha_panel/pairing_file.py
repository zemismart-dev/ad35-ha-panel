"""Read one private pairing file without retaining its upload or exposing its key."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import stat

from .protocol import (
    PanelError, normalize_mac, strict_json, validate_host, validate_noise_psk,
)

MAX_PAIRING_FILE_SIZE = 4096
PAIRING_FILE_FIELDS = {"format", "version", "mac", "noise_psk", "host"}


@dataclass(frozen=True, repr=False)
class PairingFile:
    mac: str
    noise_psk: str = field(repr=False)
    host: str


def validate_pairing_file(raw: bytes) -> PairingFile:
    """Parse a bounded UTF-8 document; all failures have the same safe error."""
    try:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_PAIRING_FILE_SIZE:
            raise ValueError
        data = strict_json(raw.decode("utf-8"))
        if (not isinstance(data, dict) or set(data) != PAIRING_FILE_FIELDS
                or data["format"] != "ad35-ha-panel-pairing"
                or type(data["version"]) is not int or data["version"] != 1):
            raise ValueError
        # Exported files use lowercase hex. Also accept the usual display forms,
        # then bind the same canonical identity used by the encrypted client.
        mac = data["mac"]
        if not isinstance(mac, str) or not re.fullmatch(
                r"(?:[0-9a-fA-F]{12}|(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}|"
                r"(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})", mac):
            raise ValueError
        host = validate_host(data["host"])
        # The card carries a hostname/IP, never a URL or an empty DNS label.
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
               for label in host.split(".")):
            raise ValueError
        return PairingFile(normalize_mac(mac), validate_noise_psk(data["noise_psk"]), host)
    except Exception:
        raise PanelError("invalid_pairing_file") from None


def load_uploaded_pairing_file(hass, file_id: str) -> PairingFile:
    """Consume an HA upload. Run this entire wrapper in async_add_executor_job.

    Core 2026.8 process_uploaded_file deletes its tracked file and directory on
    context exit, including parse failures. Upload IDs are opaque 32-digit hex;
    filenames and filesystem paths must never be supplied to this interface.
    """
    try:
        if not isinstance(file_id, str) or not re.fullmatch(r"[0-9a-f]{32}", file_id):
            raise ValueError
        from homeassistant.components.file_upload import process_uploaded_file

        with process_uploaded_file(hass, file_id) as path:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as file:
                metadata = os.fstat(file.fileno())
                if (not stat.S_ISREG(metadata.st_mode)
                        or not 0 < metadata.st_size <= MAX_PAIRING_FILE_SIZE):
                    raise ValueError
                raw = file.read(MAX_PAIRING_FILE_SIZE + 1)
            return validate_pairing_file(raw)
    except Exception:
        raise PanelError("invalid_pairing_file") from None
