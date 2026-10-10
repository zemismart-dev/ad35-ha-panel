"""Explicit local-file consent, bounded transfer, and authenticated boot proof."""
from __future__ import annotations

import asyncio
import base64
import secrets
import time

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .local_firmware import HEADER_SIZE, load_uploaded_firmware
from .local_protocol import LAST_ERRORS, LocalTransport
from .protocol import PanelError
from .update_protocol import version_tuple

POLL_DELAY = 2
TRANSFER_TIMEOUT = 600
BOOT_TIMEOUT = 180
TRANSIENT = {"cannot_connect", "commissioning_unavailable", "native_api_failed", "device_rejected",
             "not_ready", "local_not_ready"}
SAFE_ERRORS = {"local_" + item for item in LAST_ERRORS} | {
    "local_requires_firmware", "local_invalid_image", "local_downgrade", "local_wrong_device",
    "local_result_unknown", "local_transfer_failed", "local_expired", "local_confirm_required",
    "local_unavailable", "identity_mismatch", "commissioning_certificate_error", "invalid_response"}


def safe_error(error):
    return error.code if isinstance(error, PanelError) and error.code in SAFE_ERRORS else "local_unavailable"


class LocalInstaller:
    def __init__(self, hass, entry, coordinator):
        self.hass, self.entry, self.coordinator = hass, entry, coordinator
        self.transport = LocalTransport(hass, entry)
        self.image = None
        self.request_id = secrets.token_hex(32)
        self.target_partition = 0
        self.finish_sent = False
        self.confirmed_boot = False
        self.deadline = 0

    async def _read(self):
        try:
            return await self.transport.rpc("local_info", {})
        except PanelError as error:
            if error.code not in TRANSIENT:
                raise
            await self.transport.disconnect()
            raise

    def _receipt(self, data):
        if (data["request_id"] != self.request_id or data["size"] != self.image.size
                or data["sha256"] != self.image.sha256 or data["target_version"] != self.image.version
                or not data["target_partition"]):
            raise PanelError("local_transfer_failed")
        if self.target_partition and data["target_partition"] != self.target_partition:
            raise PanelError("local_transfer_failed")
        self.target_partition = data["target_partition"]
        if data["last_error"]:
            raise PanelError("local_" + data["last_error"])
        return data

    async def _observe(self, *, offset=None):
        while time.monotonic() < self.deadline:
            try:
                data = self._receipt(await self._read())
            except PanelError as error:
                if error.code not in TRANSIENT:
                    raise
            else:
                if data["phase"] == "receiving":
                    if offset is not None and data["received"] != offset:
                        raise PanelError("local_offset_mismatch")
                    return data
                if data["phase"] not in {"preparing"}:
                    raise PanelError("local_transfer_failed")
            await asyncio.sleep(POLL_DELAY)
        raise PanelError("local_expired")

    async def _write_once(self, operation, payload):
        try:
            return await self.transport.rpc(operation, payload)
        except PanelError as error:
            if error.code not in TRANSIENT:
                raise
            # A lost acknowledgement is not permission to replay a flash write.
            await self.transport.disconnect()
            return None

    async def _boot(self, old_boot):
        deadline = time.monotonic() + BOOT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                data = await self._read()
            except PanelError as error:
                if error.code not in TRANSIENT:
                    raise
            else:
                if data["boot_id"] != old_boot:
                    if (data["running_partition"] != self.target_partition
                            or data["current_version"] != self.image.version
                            or data["current_image_sha256"] != self.image.image_sha256):
                        raise PanelError("local_boot_failed")
                    self.confirmed_boot = True
                    return data
                if data["request_id"] == self.request_id and data["last_error"]:
                    raise PanelError("local_" + data["last_error"])
            await asyncio.sleep(POLL_DELAY)
        raise PanelError("local_result_unknown")

    async def run(self, image):
        self.image = image
        co = self.coordinator
        try:
            if co._closed or co.command_lock.locked():
                raise PanelError("local_busy")
            async with co.command_lock:
                co._command_task = asyncio.current_task()
                co.installing, co.last_error = True, ""
                co._publish()
                try:
                    before = await self.transport.connect()
                    if before["busy"]:
                        raise PanelError("local_busy")
                    if image.size > before["max_size"]:
                        raise PanelError("local_size_mismatch")
                    if version_tuple(image.version) < version_tuple(before["current_version"]):
                        raise PanelError("local_downgrade")
                    self.deadline = time.monotonic() + TRANSFER_TIMEOUT
                    data = await self._write_once("local_begin", {
                        "request_id": self.request_id, "size": image.size, "sha256": image.sha256,
                        "version": image.version, "header": base64.b64encode(image.data[:HEADER_SIZE]).decode("ascii")})
                    if data is not None:
                        self._receipt(data)
                    data = await self._observe(offset=0)
                    chunk_size = min(before["chunk_size"], data["chunk_size"])
                    for offset in range(0, image.size, chunk_size):
                        if time.monotonic() >= self.deadline:
                            raise PanelError("local_expired")
                        chunk = image.data[offset:offset + chunk_size]
                        response = await self._write_once("local_chunk", {
                            "request_id": self.request_id, "offset": offset,
                            "data": base64.b64encode(chunk).decode("ascii")})
                        if response is None:
                            await self._observe(offset=offset + len(chunk))
                        else:
                            self._receipt(response)
                            if response["phase"] != "receiving" or response["received"] != offset + len(chunk):
                                raise PanelError("local_offset_mismatch")
                    self.finish_sent = True
                    response = await self._write_once("local_finish", {"request_id": self.request_id})
                    if response is not None:
                        self._receipt(response)
                        if response["phase"] not in {"verifying", "rebooting"}:
                            raise PanelError("local_transfer_failed")
                    result = await self._boot(before["boot_id"])
                    co._sync_device_version(result)
                    if co.data is not None:
                        # The authenticated boot is current immediately. Any old cloud
                        # candidate is now stale; only a later explicit check offers it.
                        co._publish({**co.data, "current_version": result["current_version"],
                            "latest_version": "", "summary": "", "status": "", "candidate_id": "",
                            "can_install": False, "expires_in": 0, "phase": "idle", "busy": False,
                            "request_id": "", "last_error": ""})
                    return result
                except PanelError as error:
                    co.last_error = safe_error(error)
                    raise
                except asyncio.CancelledError:
                    co.last_error = "local_result_unknown" if self.finish_sent else "local_interrupted"
                    raise
                finally:
                    co.installing = False
                    co._command_task = None
                    co._publish()
        finally:
            # Cancellation/unload only closes transport: never interrupt device verification.
            self.image = None
            image = None
            await self.transport.close()


class LocalOptionsFlow(config_entries.OptionsFlow):
    """Does not save options or alter the bound entry/entity selection."""
    def __init__(self):
        self._image = None
        self._info = None
        self._task = None
        self._installer = None
        self._confirmed = False
        self._presented = False
        self._removed = False
        self._expiry = None
        self._reason = "local_unavailable"
        self._probe = None

    def _coordinator(self):
        co = getattr(self.config_entry, "runtime_data", None)
        if co is None or co._closed:
            raise PanelError("local_unavailable")
        if co.command_lock.locked():
            raise PanelError("local_busy")
        return co

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["local_upload"])

    async def async_step_local_upload(self, user_input=None):
        errors = {}
        try:
            co = self._coordinator()
            if self._info is None:
                transport = LocalTransport(self.hass, self.config_entry)
                self._probe = transport
                try:
                    self._info = await transport.connect()
                    if self._info["busy"]:
                        raise PanelError("local_busy")
                finally:
                    await transport.close()
                    self._probe = None
            if self._removed:
                return self.async_abort(reason="local_interrupted")
            if user_input is not None and user_input.get("firmware_file"):
                image = await self.hass.async_add_executor_job(
                    load_uploaded_firmware, self.hass, user_input["firmware_file"],
                    self.config_entry.unique_id, self._info["current_version"], self._info["max_size"])
                if self._removed or co._closed:
                    return self.async_abort(reason="local_interrupted")
                self._image = image
                self._presented = False
                if self._expiry is not None:
                    self._expiry.cancel()
                self._expiry = asyncio.get_running_loop().call_later(600, self._expire_image)
                return await self.async_step_local_confirm()
        except PanelError as error:
            if error.code == "local_requires_firmware":
                return self.async_abort(reason=error.code)
            errors["base"] = safe_error(error)
        return self.async_show_form(step_id="local_upload", data_schema=vol.Schema({
            vol.Required("firmware_file"): selector.FileSelector(selector.FileSelectorConfig(accept=".bin"))}),
            errors=errors, last_step=False)

    def _expire_image(self):
        self._image = None

    async def async_step_local_confirm(self, user_input=None):
        if self._image is None or self._removed:
            return self.async_abort(reason="local_expired")
        errors = {}
        if self._presented and user_input is not None:
            if user_input.get("confirm_install") is True:
                self._confirmed = True
                if self._expiry is not None:
                    self._expiry.cancel()
                return await self.async_step_local_install()
            errors["base"] = "local_confirm_required"
        self._presented = True
        return self.async_show_form(step_id="local_confirm", data_schema=vol.Schema({
            vol.Required("confirm_install", default=False): bool}), errors=errors,
            description_placeholders={"name": self.config_entry.title, "current_version": self._info["current_version"],
                "target_version": self._image.version, "size": str(self._image.size), "sha256": self._image.sha256},
            last_step=False)

    async def async_step_local_install(self, user_input=None):
        if not self._confirmed or self._removed:
            return self.async_abort(reason="local_confirm_required")
        if self._task is None:
            try:
                co = self._coordinator()
                self._installer = LocalInstaller(self.hass, self.config_entry, co)
                image, self._image = self._image, None
                self._task = self.hass.async_create_task(self._run_install(image), "AD35 local firmware transfer")
            except PanelError as error:
                return self.async_abort(reason=safe_error(error))
        if not self._task.done():
            return self.async_show_progress(step_id="local_install", progress_action="local_install", progress_task=self._task)
        try:
            self._reason = self._task.result()
        except (Exception, asyncio.CancelledError) as error:
            self._reason = safe_error(error)
        return self.async_show_progress_done(next_step_id="local_result")

    async def _run_install(self, image):
        try:
            await self._installer.run(image)
            return "local_success"
        except Exception as error:
            # Keep only a fixed code, never exception frames retaining uploaded bytes.
            return safe_error(error)
        finally:
            image = None

    async def async_step_local_result(self, user_input=None):
        # An abort-style result deliberately leaves entry.options/configuration untouched.
        return self.async_abort(reason=self._reason)

    @callback
    def async_remove(self):
        self._removed = True
        self._image = None
        if self._expiry is not None:
            self._expiry.cancel()
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._probe is not None:
            self.hass.async_create_task(self._probe.close(), "AD35 local probe cleanup")
