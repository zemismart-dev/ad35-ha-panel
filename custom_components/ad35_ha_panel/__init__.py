"""AD35 configuration integration; the screen owns its HA connection and tokens."""

from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, MODEL
from .oauth import ensure_views
from .protocol import PanelError, normalize_mac


async def async_setup(hass, config):
    ensure_views(hass)
    return True


async def async_setup_entry(hass, entry):
    ensure_views(hass)
    try:
        mac = normalize_mac(entry.data.get("mac", ""))
    except PanelError:
        return False
    if entry.unique_id != mac or entry.data.get("model") != MODEL:
        return False
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, mac)},
        manufacturer="AD35", model=MODEL,
        name=entry.data.get("friendly_name") or entry.data.get("name") or f"AD35 {mac[-6:].upper()}",
        sw_version=entry.data.get("firmware"),
    )
    await _setup_update_platforms(hass, entry)
    return True


async def _setup_update_platforms(hass, entry):
    from homeassistant.const import Platform
    from .coordinator import FirmwareCoordinator

    coordinator = entry.runtime_data = FirmwareCoordinator(hass, entry)
    try:
        # An offline screen or older firmware must not remove the existing entry.
        # This reads local device state only; no cloud check runs during setup.
        await coordinator.async_refresh()
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.UPDATE, Platform.BUTTON])
    except BaseException:
        await coordinator.async_close()
        raise


async def async_unload_entry(hass, entry):
    # Removing this HA entry never erases a screen-side binding.
    from homeassistant.const import Platform
    unloaded = await hass.config_entries.async_unload_platforms(entry, [Platform.UPDATE, Platform.BUTTON])
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded
