# Third-party code

`custom_components/ad35_ha_panel/vendor/esp_prov/` contains selected Espressif
ESP-IDF v5.5.5 provisioning client code under Apache-2.0. The upstream license is
preserved in that directory's `LICENSE` file. `PROVENANCE.json` records upstream
URLs, source hashes, local hashes, and modifications.

The integration depends on `aioesphomeapi==45.6.1`, installed by Home Assistant.
It is not bundled here. The public commissioning CA certificate verifies device
identity; it does not contain a private key or a device credential.
