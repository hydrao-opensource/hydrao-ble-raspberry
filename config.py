from pathlib import Path

_HERE = Path(__file__).parent

SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"

CHAR = {
    "uuid":           "0000ca28-0000-1000-8000-00805f9b34fb",
    "fw_version":     "00002a26-0000-1000-8000-00805f9b34fb",
    "hw_version":     "0000ca24-0000-1000-8000-00805f9b34fb",
    "live_volume":    "0000ca1c-0000-1000-8000-00805f9b34fb",
    "live_flow":      "0000ca31-0000-1000-8000-00805f9b34fb",
    "live_temp":      "0000ca32-0000-1000-8000-00805f9b34fb",
    "vmot":           "0000ca27-0000-1000-8000-00805f9b34fb",
    "thresholds":     "0000ca1d-0000-1000-8000-00805f9b34fb",
    "calibration":    "0000ca30-0000-1000-8000-00805f9b34fb",
    "shower_range":   "0000ca21-0000-1000-8000-00805f9b34fb",
    "shower_request": "0000ca22-0000-1000-8000-00805f9b34fb",
    "shower_data":    "0000ca23-0000-1000-8000-00805f9b34fb",
    "reset_volume":   "0000ca20-0000-1000-8000-00805f9b34fb",
    "reset_cmd":      "0000ca1f-0000-1000-8000-00805f9b34fb",
}

DEVICE_NAME_PREFIX = "HYDRAO_SHOWER"
SCAN_TIMEOUT = 20           # seconds
CONNECT_TIMEOUT = 5         # seconds
LIVE_POLL_INTERVAL = 1      # seconds
MIN_VALID_VOLUME = 3        # liters
DEFAULT_CALIBRATION = 545
MAX_SOAPING_TIME = 180      # seconds

DEFAULT_THRESHOLDS = [
    {"color": "#00FF00", "liter": 5.0},
    {"color": "#0000FF", "liter": 10.0},
    {"color": "#FF00FF", "liter": 15.0},
    {"color": "#FF0000", "liter": 20.0},
]

DEFAULT_FLOW_BY_TYPE = {
    "aloe":    12.0,
    "cereus":  12.0,
    "yucca":   20.0,
    "first":   12.0,
    "mixer":   12.0,
    "unknown": 12.0,
}
LEGACY_FLOW = 6.8           # L/min for hwVersion < 8

DB_PATH = str(_HERE / "hydrao.db")
API_PORT = 8080

SCAN_IDLE_INTERVAL = 5  # seconds between scan cycles

# WiFi provisioning BLE service
WIFI_SERVICE_UUID     = "12340000-1234-1234-1234-1234567890ab"
WIFI_SSID_CHAR_UUID   = "12340001-1234-1234-1234-1234567890ab"
WIFI_PWD_CHAR_UUID    = "12340002-1234-1234-1234-1234567890ab"
WIFI_CMD_CHAR_UUID    = "12340003-1234-1234-1234-1234567890ab"
WIFI_STATUS_CHAR_UUID = "12340004-1234-1234-1234-1234567890ab"

# Historical shower IDs are stored as: index_cycle_count * 65536 + device_id (uint16).
# Live-recorded showers use a large offset to stay clear of any realistic cycle count.
LIVE_SHOWER_ID_OFFSET = 10_000_000
