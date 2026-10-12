# AD35 HA Panel

Home Assistant integration for AD35-S3 320×480 desktop control panels. Add panels, choose the entities shown on each screen, and manage panel firmware updates from Home Assistant.

[Zemismart website](https://www.zemismart.com/) · [简体中文](README.zh-CN.md) · [Latest release](https://github.com/zemismart-dev/ad35-ha-panel/releases/latest) · [Changelog](CHANGELOG.md)

## About Zemismart

Zemismart offers smart home products including smart switches, motors for blinds and curtains, lighting, sensors, and gateways. Explore the product range at [www.zemismart.com](https://www.zemismart.com/).

[![Open AD35 HA Panel in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=zemismart-dev&repository=ad35-ha-panel&category=integration)

**[Install through HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=zemismart-dev&repository=ad35-ha-panel&category=integration)**

## First installation

First [install and configure HACS](https://www.hacs.xyz/docs/use/) in your Home Assistant instance. This integration is installed as a **HACS custom repository** and is not included in the HACS default repository list.

1. Click the installation button above, follow the prompts to open your Home Assistant instance, and add the repository.
2. Open **AD35 HA Panel** in HACS and select **Download**.
3. **Restart Home Assistant.**
4. Open **Settings → Devices & services** and configure the discovered AD35 panel. Alternatively, select **Add integration** and search for **AD35 HA Panel**.

No `configuration.yaml` changes are needed. One Home Assistant instance can manage multiple panels, each with its own binding and display selection.

### If the button does not open

In HACS, open **⋮ → Custom repositories** and enter:

| Field | Value |
| --- | --- |
| Repository | `https://github.com/zemismart-dev/ad35-ha-panel` |
| Category | Integration |

Select **Add**, search for **AD35 HA Panel**, download it, and restart Home Assistant. See the [HACS custom repository guide](https://www.hacs.xyz/docs/faq/custom_repositories/).

### Manual installation without HACS

1. Download `ad35-ha-panel-integration-0.9.3.zip` from [Releases](https://github.com/zemismart-dev/ad35-ha-panel/releases/latest) and extract it.
2. Copy its `custom_components/ad35_ha_panel` folder into `custom_components` in your Home Assistant configuration directory. The final path must be `/config/custom_components/ad35_ha_panel/manifest.json`; for HA Container, use the corresponding mounted configuration directory. Do not add an extra folder level.
3. Restart Home Assistant, then add the integration under **Settings → Devices & services**.

## Compatibility

| Item | Requirement |
| --- | --- |
| Home Assistant | Core **2026.8.0 or later**; the current validation baseline is 2026.8.0 |
| Device | AD35-S3 320×480 panel running the matching AD35 HA Panel firmware |
| Recommended firmware | **2.8.1 or later**, which includes the HA authorization refresh fix |
| Network | Home Assistant and the panel must be mutually reachable on a trusted LAN; the panel must be connected to Wi-Fi |

The integration and panel firmware use separate version numbers. This repository publishes **HA integration 0.9.3**. It does not include panel firmware, device secrets, pairing files, or personal Home Assistant configuration.

## Add a panel and choose its content

Home Assistant automatically discovers an unbound panel after it connects to Wi-Fi. Firmware 2.3.1 and later keeps initial setup available, including after a restart.

1. Open **Settings → Devices & services**, find the discovered AD35 panel, and select **Configure**. If there is no discovery card, select **Add integration → AD35 HA Panel → Automatic setup** and enter the panel's current IP and MAC address.
2. Check the Home Assistant address. Both the browser and panel must be able to reach it. Do not use `localhost`, a container-internal address, or a page URL containing `/lovelace`.
3. Follow the prompts to sign in or authorize access to Home Assistant, return to the configuration dialog, select display entities and weather, and confirm saving.

Automatic setup runs entirely in Home Assistant without an approval tap on the panel. It requires firmware 2.3.0 or later, uses HTTPS on port 8443, and validates the device certificate and MAC address. Firmware 2.3.0 still has a five-minute initial setup window, so upgrading the firmware first is recommended. Temporary-code setup requires firmware 2.2.0 or later; pairing-file or manual setup requires 2.1.2 or later. Pairing files are supplied separately by the device provider.

For a panel that is already bound, use the existing entry's **⋮ → Reconfigure** action. **You do not need to delete the entry, unbind the panel, or add it again** to upgrade the integration or change the displayed content.

### Supported display entities

Select up to **64 display entities plus one weather entity**. Available controls depend on the capabilities supported by each Home Assistant entity.

| Entity type | Panel functions |
| --- | --- |
| Lights | On/off, brightness, color, and color temperature |
| Switches | On/off control |
| Covers | Open, close, stop, and position control where supported |
| Climate devices | Mode, temperature, fan speed, and swing |
| Sensors and binary sensors | State display |
| Locks | Lock and unlock; unlocking requires confirmation on the panel. Operate locks that require a code in Home Assistant |
| Cameras | Tap to view a still image with manual refresh; live video is not provided |
| Vacuums | Cleaning controls supported by the entity |
| Weather | Weather on the home screen |

**Both the display selection and weather can be left empty** with firmware 2.7.2 or later. Firmware 2.8.1 displays a clock and date on an empty home screen; selecting weather adds weather information.

The **Home layout · Positions 1–6** selectors order the selected entities. Do not select the same entity twice. Leave unspecified positions on **Automatic fill**. These settings control order and do not reserve blank spaces. Firmware 2.8.1 adjusts the home layout to the number of selected entities.

## Update the integration and panel firmware

### Update the Home Assistant integration

Download the new version in HACS, then restart Home Assistant. For a manual installation, back up the existing `ad35_ha_panel` folder, replace it with the new version, and restart. **Keep the existing AD35 entries and panel bindings in Home Assistant.**

Version 0.9.3 adds metadata, brand assets, and installation documentation for GitHub/HACS distribution. Runtime behavior is unchanged from 0.9.2, and existing 0.9.2 installations can upgrade directly.

### Update panel firmware

HACS updates the Home Assistant integration; **it does not install firmware on the panel**. Use the separate firmware update options:

- **Cloud firmware:** Open the relevant AD35 device in Home Assistant, select **Check firmware updates**, review the version in the **Firmware** card, and select **Install**. This requires panel firmware 2.4.0 or later. For older firmware, first update once through the panel's **Settings → Firmware update** menu or the original maintenance method.
- **Local `.bin` file:** Open **Settings → Devices & services → AD35 HA Panel → the relevant entry's options → Local firmware update**. Upload the OTA `.bin` supplied for this particular panel, review its version and digest, and confirm. This requires firmware 2.5.0 or later. `factory.bin`, files for other devices, and downgrade files are not accepted.

Keep power and network connectivity available during installation; the panel will restart. Firmware updates preserve the Home Assistant binding and selected content. If an update result is not yet confirmed, check the panel's current version before deciding whether to retry. During normal operation, the integration does not install panel firmware automatically.

## Troubleshooting

**The integration cannot be found after installation**  
Restart Home Assistant and refresh the browser. For a manual installation, check the folder level containing `manifest.json`.

**The panel is not discovered automatically**  
Confirm that the panel is connected to Wi-Fi and that Home Assistant and the panel can reach each other. Across VLANs, configure local discovery and access rules correctly. You can also use **Automatic setup** and enter the panel IP and MAC address.

**No new discovery card appears after unbinding on the panel**  
Home Assistant may still have the original entry. Entries created with automatic setup usually show a reauthentication prompt within about 60 seconds. You can also open the original entry's **⋮ → Reconfigure → Reconnect** action. Authorize again and save; there is no need to delete the original device or its history.

**Saving was interrupted**  
Use the original entry's reconfiguration flow or **Recover saved configuration** to check the actual result and avoid duplicate submissions.

**Home Assistant authorization fails after a period of normal use**  
Check that the panel itself is running firmware 2.8.1 or later. The authorization refresh fix is in the panel firmware; updating only the Home Assistant integration does not update panel firmware.

If you still need help, open an [issue](https://github.com/zemismart-dev/ad35-ha-panel/issues) with the integration, Home Assistant, and firmware versions and steps to reproduce the problem. Remove tokens, pairing-file contents, and device secrets before sharing logs.
