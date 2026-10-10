# AD35 HA Panel

Home Assistant integration for AD35-S3 320×480 desktop control panels. Add panels, select the entities shown on each screen, and manage panel firmware updates from Home Assistant.

[中文说明](README.md) · [Latest release](https://github.com/zemismart-dev/ad35-ha-panel/releases/latest) · [Changelog](CHANGELOG.md)

[![Open AD35 HA Panel in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=zemismart-dev&repository=ad35-ha-panel&category=integration)

## Install with HACS

Requires [HACS installed and configured](https://www.hacs.xyz/docs/use/) in your Home Assistant. This is a **HACS custom repository**, not a repository in the HACS default list.

1. Click the button above, open your Home Assistant instance, and add the repository.
2. Open **AD35 HA Panel** in HACS and select **Download**.
3. **Restart Home Assistant.**
4. Open **Settings → Devices & services** and configure the discovered panel, or select **Add integration → AD35 HA Panel**.

If the button does not work, open **HACS → ⋮ → Custom repositories**, enter `https://github.com/zemismart-dev/ad35-ha-panel`, and select **Integration**. Add it, download the integration, then restart Home Assistant. No `configuration.yaml` changes are needed.

## Manual installation

Download `ad35-ha-panel-integration-0.9.3.zip` from the [latest release](https://github.com/zemismart-dev/ad35-ha-panel/releases/latest). Extract it and copy `custom_components/ad35_ha_panel` into your HA configuration directory. The final path should be `/config/custom_components/ad35_ha_panel/manifest.json` (or the corresponding mounted configuration directory for HA Container). Restart Home Assistant.

## Requirements and setup

- Home Assistant Core **2026.8.0 or later**; the current validation baseline is 2026.8.0.
- AD35-S3 320×480 hardware running the matching AD35 HA Panel firmware. Firmware **2.8.1 or later** is recommended for the HA authorization refresh fix.
- The panel must be connected to Wi-Fi, and Home Assistant and the panel must be reachable from each other on a trusted local network.

An unbound panel is discovered automatically. Firmware 2.3.1 and later keeps initial setup available after connecting to Wi-Fi, including after a restart. If discovery is unavailable, use **Add integration → AD35 HA Panel → Automatic setup** and enter the panel IP and MAC address.

Confirm a Home Assistant address accessible to both the browser and panel, authorize access, choose the display entities, and confirm saving. Automatic setup runs entirely in HA without a screen approval tap. It uses HTTPS on port 8443 and validates the device certificate and MAC. Firmware 2.3.0 supports automatic setup within a five-minute initial setup window; temporary-code setup requires 2.2.0 or later, and pairing-file/manual setup requires 2.1.2 or later.

Use the existing integration entry's **Reconfigure** action to change displayed entities or reconnect an unbound panel. Keep the existing HA entry when upgrading; do not delete it or unbind the panel.

## Display and control

Select up to **64 display entities plus one weather entity**. Supported types are lights, switches, covers, climate devices, sensors, binary sensors, locks, cameras, vacuums, and weather. Available controls follow each entity's capabilities, including light color/color temperature and climate fan/swing modes. Cameras show manually refreshed still images. Unlocking requires confirmation on the panel; locks requiring a code should be operated in HA.

Both the display selection and weather can be empty with firmware 2.7.2 or later. Firmware 2.8.1 shows a clock and date when empty. The six home-position selectors arrange selected entities; unspecified positions fill automatically and do not reserve blank slots.

## Updates

**HA integration:** Download updates in HACS and restart HA, or replace the integration folder manually after backing it up. Keep existing entries and bindings. Integration 0.9.3 adds GitHub/HACS metadata, brand assets, and installation documentation; runtime behavior is unchanged from 0.9.2.

**Panel firmware:** HACS does not install firmware on the screen. Open the panel device in HA, run **Check firmware updates**, then use its firmware update entity to review and install a cloud release (firmware 2.4.0 or later). For a local file, open the integration entry's options and choose **Local firmware update** (firmware 2.5.0 or later). Upload only the OTA `.bin` provided for that particular panel, not a factory image or a file for another device. Downgrades are not supported.

Keep the panel powered and connected during installation. Firmware updates preserve the HA binding and display selection. If the result is uncertain, check the panel's current version before retrying. Firmware is never installed automatically during normal operation.

The integration and firmware have separate version numbers. This repository contains **integration 0.9.3**, not firmware images, device secrets, pairing files, or personal HA configuration.

For troubleshooting and detailed display options, see the [Chinese guide](README.md). Report problems in [Issues](https://github.com/zemismart-dev/ad35-ha-panel/issues) with your HA, integration, and firmware versions and reproduction steps. Remove tokens and device secrets from logs before sharing them.
