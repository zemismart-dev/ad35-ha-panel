"""Protocol constants shared by compatible AD35 panels."""

DOMAIN = "ad35_ha_panel"
ALLOWLIST_FILE = "ad35_panel_devices.json"
PROJECT = "local.ad35_ha_panel"
NATIVE_MODEL = "esp32-s3"
MODEL = "ad35-s3-lcd1-16m"
PORT = 6053
SERVICE = "ad35_pair_rpc"
MIN_FIRMWARE = (2, 1, 2)
CALLBACK_PATH = "/api/ad35_ha_panel/auth/callback"
CLIENT_PATH = "/api/ad35_ha_panel/client/"
# Ordinary screen items include read-only domains; weather has its own slot.
SELECTABLE_DOMAINS = frozenset({"light", "switch", "cover", "climate", "sensor",
                              "binary_sensor", "lock", "camera", "vacuum"})
# Compatibility for callers of the original public constant.
CONTROL_DOMAINS = SELECTABLE_DOMAINS

BOOTSTRAP_PORT = 8081
MIN_PIN_FIRMWARE = (2, 2, 0)
COMMISSIONING_PORT = 8443
MIN_COMMISSIONING_FIRMWARE = (2, 3, 0)
MIN_EMPTY_SELECTION_FIRMWARE = (2, 7, 2)
COMMISSIONING_TRANSPORT = "https-v1"
