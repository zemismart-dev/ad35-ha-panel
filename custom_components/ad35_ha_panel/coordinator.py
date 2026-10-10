"""Read-only status polling and bounded, never-replayed update commands."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import secrets
import time

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import COMMISSIONING_TRANSPORT, DOMAIN, MODEL
from .commissioning import verified_unbound_info
from .protocol import PanelError
from .update_protocol import UpdateTransport, version_tuple

LOGGER = logging.getLogger(__name__)
INSTALL_TIMEOUT = 600
CHECK_TIMEOUT = 45
POLL_DELAY = 3
INSTALL_PHASES = {"install_queued", "installing", "succeeded", "external_ota"}
TRANSIENT = {"cannot_connect", "commissioning_unavailable", "native_api_failed", "device_rejected", "not_ready"}
READ_TRANSIENT = TRANSIENT | {"update_busy", "update_not_ready"}


def service_error(code):
    from .update_protocol import ERRORS
    safe = {"update_" + item for item in ERRORS} | {
        "update_requires_firmware", "update_check_timeout", "update_result_unknown", "update_invalid_options",
        "identity_mismatch", "commissioning_certificate_error", "invalid_response", "invalid_credential"}
    return HomeAssistantError(translation_domain=DOMAIN, translation_key=code if code in safe else "update_unavailable")


class FirmwareCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(hass, LOGGER, config_entry=entry, name=f"AD35 firmware {entry.unique_id}",
                         update_interval=timedelta(seconds=60), always_update=False)
        self.transport = UpdateTransport(hass, entry)
        self.command_lock = asyncio.Lock()
        self.installing = False
        self.last_error = ""
        self._closed = False
        self._command_task = None
        self._offered = None

    def _sync_device_version(self, data):
        # Replies have passed TLS/Noise identity and strict update decoding.
        # Keep the device page current without changing binding data or reloading.
        if data["mac"] == self.config_entry.unique_id and data["model"] == MODEL:
            registry = dr.async_get(self.hass)
            device = registry.async_get_device(identifiers={(DOMAIN, self.config_entry.unique_id)})
            if device is not None and device.sw_version != data["current_version"]:
                registry.async_update_device(device.id, sw_version=data["current_version"])

    def _remember(self, data):
        self._sync_device_version(data)
        if data["candidate_id"]:
            self._offered = (data["latest_version"], data["candidate_id"])
        elif self._offered is not None and data["latest_version"] != self._offered[0]:
            self._offered = None
        return data

    async def _async_update_data(self):
        # Installation owns its bounded polling. Never turn a status poll into CHECK.
        if self.command_lock.locked() and self.data is not None:
            return self.data
        try:
            async with self.command_lock:
                return self._remember(await self.transport.rpc("update_info", {}))
        except PanelError as error:
            # Never turn offline/TLS failures or a different live binding into
            # anonymous setup. Core deduplicates the reauth prompt and stops
            # polling after this exception, including during async_refresh setup.
            if (not self._closed and self.config_entry.data.get("transport") == COMMISSIONING_TRANSPORT
                    and error.code in {"invalid_credential", "update_invalid_credential"}):
                try:
                    await verified_unbound_info(self.config_entry.data["host"], self.config_entry.unique_id)
                except PanelError:
                    pass
                else:
                    if not self._closed:
                        raise ConfigEntryAuthFailed("Screen was unbound; reconnect in Home Assistant") from None
            raise UpdateFailed("Device update status unavailable") from None

    def _publish(self, data=None):
        if self._closed:
            return
        if data is not None:
            self._remember(data)
            self.async_set_updated_data(data)
        else:
            self.async_update_listeners()

    async def _read(self):
        data = await self.transport.rpc("update_info", {})
        self._publish(data)
        return data

    def _require_ready(self, data):
        if data.get("last_error") == "requires_firmware":
            raise PanelError("update_requires_firmware")
        if not data["configured"]:
            raise PanelError("update_not_configured")
        if data["busy"]:
            raise PanelError("update_busy")

    async def _wait(self, deadline, request_id, target=None):
        while not self._closed and time.monotonic() < deadline:
            await asyncio.sleep(POLL_DELAY)
            try:
                data = await self._read()
            except PanelError as error:
                if error.code in READ_TRANSIENT:
                    continue
                raise
            if target is not None and data["current_version"] == target:
                return data
            if data["request_id"] != request_id:
                continue
            if data["last_error"]:
                raise PanelError("update_" + data["last_error"])
            if target is None and data["phase"] == "idle":
                return data
            # An idle old version after install is not proof of success. Wait for reboot.
        raise PanelError("update_result_unknown" if target else "update_check_timeout")

    async def _check(self):
        self._require_ready(await self._read())
        request_id = secrets.token_hex(32)
        try:
            self._publish(await self.transport.rpc("update_check", {"request_id": request_id}))
        except PanelError as error:
            if error.code not in TRANSIENT:
                raise
        return await self._wait(time.monotonic() + CHECK_TIMEOUT, request_id)

    async def async_check(self):
        if self.command_lock.locked() or self._closed:
            raise service_error("update_busy")
        async with self.command_lock:
            self._command_task = asyncio.current_task()
            self.last_error = ""
            try:
                return await self._check()
            except PanelError as error:
                self.last_error = error.code
                raise service_error(error.code) from None
            finally:
                self._command_task = None
                self._publish()

    async def async_install(self):
        if self.command_lock.locked() or self._closed:
            raise service_error("update_busy")
        target = (self.data or {}).get("latest_version")
        candidate_id = (self.data or {}).get("candidate_id")
        if not candidate_id and self._offered is not None and self._offered[0] == target:
            candidate_id = self._offered[1]
        if not target or not candidate_id:
            raise service_error("update_no_candidate")
        async with self.command_lock:
            self._command_task = asyncio.current_task()
            self.last_error = ""
            sent = False
            try:
                data = await self._read()
                self._require_ready(data)
                if version_tuple(target) <= version_tuple(data["current_version"]):
                    raise PanelError("update_no_candidate")
                if not data["can_install"] or data["latest_version"] != target:
                    data = await self._check()
                if data["latest_version"] != target or data["candidate_id"] != candidate_id or not data["can_install"]:
                    raise PanelError("update_candidate_mismatch")
                request_id = secrets.token_hex(32)
                self.installing = True
                self._publish()
                sent = True
                try:
                    self._publish(await self.transport.rpc("update_install", {
                        "request_id": request_id, "version": target, "candidate_id": data["candidate_id"]}))
                except PanelError as error:
                    if error.code not in TRANSIENT:
                        raise
                await self._wait(time.monotonic() + INSTALL_TIMEOUT, request_id, target)
            except PanelError as error:
                self.last_error = error.code
                raise service_error(error.code) from None
            except asyncio.CancelledError:
                if sent:
                    self.last_error = "update_result_unknown"
                raise
            finally:
                self.installing = False
                self._command_task = None
                self._publish()

    async def async_close(self):
        self._closed = True
        await self.async_shutdown()
        if self._command_task is not None and self._command_task is not asyncio.current_task():
            self._command_task.cancel()
            await asyncio.gather(self._command_task, return_exceptions=True)
        await self.transport.close()
