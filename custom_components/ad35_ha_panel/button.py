"""An explicit check button; pressing it never installs firmware."""
from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([CheckFirmware(entry.runtime_data, entry)])


class CheckFirmware(CoordinatorEntity, ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "check_firmware"
    _attr_device_class = ButtonDeviceClass.UPDATE
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id}_check_firmware"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.unique_id)}}

    async def async_press(self):
        await self.coordinator.async_check()
