"""Consume and validate a device-specific ESP32-S3 OTA image, entirely off-loop.

ESP image layout/checksum: espressif/esptool v5.0.2 bin_image.py. The appended
SHA256 identifies the running image; the complete-file SHA256 verifies transfer.
Neither is the ELF build digest in esp_app_desc. No image is logged or persisted.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
import hashlib
import hmac
import operator
import os
import re
import stat
import struct

from .const import MODEL
from .protocol import PanelError
from .update_protocol import version_tuple

MAX_IMAGE_SIZE = 0x7C0000
HEADER_SIZE = 384
METADATA_MAGIC = b"AD35LOCALOTA1\0\0\0"


@dataclass(frozen=True, repr=False)
class FirmwareImage:
    data: bytes
    version: str
    sha256: str
    image_sha256: str
    size: int


def _cstring(value):
    head, separator, tail = value.partition(b"\0")
    if not separator or not head or any(tail):
        raise ValueError
    return head.decode("ascii")


def validate_firmware(raw: bytes, mac: str, current_version: str,
                      max_size: int = MAX_IMAGE_SIZE) -> FirmwareImage:
    """Strict complete OTA image, fixed custom descriptor and no trailing bytes."""
    try:
        if (not isinstance(raw, bytes) or type(max_size) is not int
                or not HEADER_SIZE < len(raw) <= min(max_size, MAX_IMAGE_SIZE)
                or raw[0] != 0xE9 or not 1 <= raw[1] <= 16 or raw[3] >> 4 != 4
                or struct.unpack_from("<H", raw, 12)[0] != 9 or raw[23] != 1
                or raw[19:23] != b"\0" * 4
                or struct.unpack_from("<I", raw, 32)[0] != 0xABCD5432
                or struct.unpack_from("<I", raw, 28)[0] < 352):
            raise ValueError
        if raw[288:304] != METADATA_MAGIC:
            raise ValueError
        model, image_mac, version = (_cstring(raw[304:336]), _cstring(raw[336:352]), _cstring(raw[352:384]))
        if model != MODEL or image_mac != mac or not re.fullmatch(r"[0-9a-f]{12}", image_mac):
            raise PanelError("local_wrong_device")
        if version_tuple(version) < version_tuple(current_version):
            raise PanelError("local_downgrade")
        position, checksum = 24, 0xEF
        for _ in range(raw[1]):
            if position + 8 > len(raw):
                raise ValueError
            length = struct.unpack_from("<I", raw, position + 4)[0]
            position += 8
            if length % 4 or position + length > len(raw):
                raise ValueError
            checksum = reduce(operator.xor, memoryview(raw)[position:position + length], checksum)
            position += length
        checksum_offset = position + (15 - position % 16)
        if (checksum_offset + 33 != len(raw) or any(raw[position:checksum_offset])
                or raw[checksum_offset] != checksum
                or not hmac.compare_digest(hashlib.sha256(raw[:-32]).digest(), raw[-32:])):
            raise ValueError
        return FirmwareImage(raw, version, hashlib.sha256(raw).hexdigest(), raw[-32:].hex(), len(raw))
    except PanelError as error:
        if error.code in {"local_wrong_device", "local_downgrade"}:
            raise
        raise PanelError("local_invalid_image") from None
    except (ValueError, TypeError, IndexError, struct.error, UnicodeError):
        raise PanelError("local_invalid_image") from None


def load_uploaded_firmware(hass, file_id, mac, current_version, max_size=MAX_IMAGE_SIZE):
    """Run the full official upload context/validation/cleanup in the executor."""
    try:
        if not isinstance(file_id, str) or not re.fullmatch(r"[0-9a-f]{32}", file_id):
            raise ValueError
        from homeassistant.components.file_upload import process_uploaded_file
        with process_uploaded_file(hass, file_id) as path:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as file:
                metadata = os.fstat(file.fileno())
                if not stat.S_ISREG(metadata.st_mode) or not HEADER_SIZE < metadata.st_size <= min(max_size, MAX_IMAGE_SIZE):
                    raise ValueError
                raw = file.read(min(max_size, MAX_IMAGE_SIZE) + 1)
            return validate_firmware(raw, mac, current_version, max_size)
    except PanelError:
        raise
    except Exception:
        raise PanelError("local_invalid_image") from None
