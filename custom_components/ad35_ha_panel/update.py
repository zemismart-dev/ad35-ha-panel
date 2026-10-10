"""Home Assistant firmware card; no invented percentage or automatic installation."""
from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import INSTALL_PHASES, service_error

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([FirmwareUpdate(entry.runtime_data, entry)])


class FirmwareUpdate(CoordinatorEntity, UpdateEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id}_firmware"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.unique_id)}}

    @property
    def installed_version(self):
        return (self.coordinator.data or {}).get("current_version")

    @property
    def latest_version(self):
        return (self.coordinator.data or {}).get("latest_version") or None

    @property
    def release_summary(self):
        data = self.coordinator.data or {}
        chinese = getattr(getattr(getattr(self, "hass", None), "config", None), "language", "en").startswith("zh")
        if data.get("last_error") == "requires_firmware":
            return ("HA 固件升级需要设备固件 2.4.0 或以上，请先通过原有维护入口升级一次。" if chinese else
                    "HA firmware updates require firmware 2.4.0 or newer. First upgrade through the existing maintenance method.")
        if data and not data.get("configured"):
            return ("桌宠尚未配置固件发布服务，暂时不能检查或安装更新。" if chinese else
                    "The screen's firmware release service is not configured. Checking and installation are unavailable.")
        if self.coordinator.last_error == "update_result_unknown":
            return ("升级结果尚未确认，没有重复安装。请等待桌宠联网后核对当前版本。" if chinese else
                    "The update result is unconfirmed. Installation was not repeated. Wait for the screen to reconnect and verify its version.")
        return data.get("summary") or None

    @property
    def in_progress(self):
        return self.coordinator.installing or (self.coordinator.last_error != "update_result_unknown"
            and (self.coordinator.data or {}).get("phase") in INSTALL_PHASES)

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        error = self.coordinator.last_error or data.get("last_error", "")
        if not error and data and not data.get("configured"):
            error = "not_configured"
        return {"update_status": data.get("status", ""), "last_error": error,
                "update_service_configured": data.get("configured", False)}

    async def async_update(self):
        # HA's explicit update_entity action, matching Core ESPHome's CHECK behavior.
        if self.enabled:
            await self.coordinator.async_check()

    async def async_release_notes(self):
        return self.release_summary

    async def async_install(self, version, backup, **kwargs):
        if version is not None or backup:
            raise service_error("update_invalid_options")
        await self.coordinator.async_install()
