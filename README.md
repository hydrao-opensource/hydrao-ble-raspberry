# hydrao-ble-raspberry

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue)](pyproject.toml)
[![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi-red)](https://www.raspberrypi.com/)

A Bluetooth Low Energy gateway for Hydrao smart shower heads, designed to run on a Raspberry Pi. It continuously scans for nearby HYDRAO devices, syncs their shower history, and exposes everything through a REST API and a WebSocket feed.

## Overview

```text
┌─────────────────────────────────────────────────┐
│                  Raspberry Pi                   │
│                                                 │
│  BLE Scanner ──► DeviceHandler ──► SQLite DB    │
│       │                                │        │
│  WiFi Provision                   FastAPI       │
│  (HYDRAO-Setup)              REST + WebSocket   │
│                                   + mDNS        │
└─────────────────────────────────────────────────┘
        ▲                          ▲
   HYDRAO shower heads         Flutter app
   (BLE peripherals)        (HTTP / WebSocket)
```

Key components:

| Module              | Role                                                                       |
| ------------------- | -------------------------------------------------------------------------- |
| `scanner.py`        | Continuous BLE scan, filters devices by name prefix `HYDRAO_SHOWER`        |
| `device.py`         | Per-device async handler: reads GATT characteristics, syncs shower history |
| `protocol.py`       | Binary encoding/decoding for all HYDRAO GATT characteristics               |
| `database.py`       | SQLite persistence via `aiosqlite`                                         |
| `api.py`            | FastAPI REST endpoints + WebSocket live feed                               |
| `mdns.py`           | mDNS advertisement so the Flutter app can discover the gateway             |
| `wifi_provision.py` | BLE GATT server (`HYDRAO-Setup`) for first-time WiFi setup                 |

## Requirements

- Raspberry Pi with a Bluetooth adapter (tested on Raspberry Pi OS Bookworm)
- Python ≥ 3.10
- BlueZ ≥ 5.43
- NetworkManager + `nmcli` (required for WiFi provisioning)
- [`uv`](https://github.com/astral-sh/uv) package manager

## Installation

```bash
git clone https://github.com/hydrao-opensource/hydrao-ble-raspberry.git
cd hydrao-ble-raspberry
uv sync
```

### Run directly

```bash
uv run python main.py
# With debug logging
uv run python main.py --debug
# Custom port
uv run python main.py --port 9090
```

### Run as a systemd service

```bash
# Copy the unit file
sudo cp service/hydrao.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hydrao
# Check status
sudo systemctl status hydrao
sudo journalctl -u hydrao -f
```

The service runs as the `pi` user (with `bluetooth` group membership) and restarts automatically on failure. It waits 5 seconds after boot for the Bluetooth adapter to be ready.

## REST API

The API listens on port **8080** by default and is discoverable on the local network as `hydrao-ble-raspberry._http._tcp.local.` via mDNS.

### Endpoints

#### `GET /devices`

Returns all HYDRAO devices seen since the collector started, sorted by last activity.

```jsonc
{
  "allowed_filter_active": true,
  "allowed_addresses": ["AA:BB:CC:DD:EE:FF"],
  "devices": [
    {
      "id": "AA:BB:CC:DD:EE:FF",
      "uuid": "13F2BAB0-FFC2-E334-80B3-7D309FE16210",
      "name": "HYDRAO_SHOWER",
      "hw_version": 8,
      "fw_version": "3.2.1",
      "calibration": 545,
      "threshold": [
        { "color": "#00FF00", "liter": 5.0 },
        { "color": "#0000FF", "liter": 10.0 },
        { "color": "#FF00FF", "liter": 15.0 },
        { "color": "#FF0000", "liter": 20.0 },
      ],
      "first_seen": "2024-01-15T08:00:00",
      "last_seen": "2024-01-20T07:45:00",
      "last_rssi": -62,
      "is_last_sync_complete": true,
      "live": {
        "volume": 0,
        "flow": null,
        "duration": null,
        "temperature": null,
        "date": null,
      },
    },
  ],
}
```

> **Note on `device_id`**: on macOS/CoreBluetooth it is a UUID (e.g. `13F2BAB0-…`); on Linux/BlueZ it is a MAC address (e.g. `AA:BB:CC:DD:EE:FF`).

#### `PUT /devices/allowed`

Replaces the device allowlist. The scanner picks up the change immediately without a restart. Send an empty list to allow all HYDRAO devices.

```jsonc
// Request
{ "addresses": ["AA:BB:CC:DD:EE:FF"] }

// Response
{ "allowed_addresses": ["AA:BB:CC:DD:EE:FF"] }
```

#### `GET /devices/{device_id}/showers`

Returns the shower history for a device, sorted by date descending. Supports `limit` (1–500, default 50) and `offset` query parameters for pagination.

```jsonc
{
  "device_id": "AA:BB:CC:DD:EE:FF",
  "showers": [
    {
      "id": 42,
      "volume": 12,
      "temperature": 38.5,
      "flow": 8.2,
      "duration": 87.8,
      "soaping_time": 24,
      "date": "2024-01-20T07:32:00",
      "threshold": [{ "color": "#00FF00", "liter": 5.0 }],
      "is_empty": false,
    },
  ],
  "limit": 50,
  "offset": 0,
}
```

#### `GET /devices/{device_id}/live`

Returns the latest real-time measurements for the ongoing (or last) shower. If no shower is in progress, `volume` is `0` and other fields are `null`.

#### `WS /ws/live`

WebSocket endpoint. The server pushes a snapshot of all devices every 2 seconds:

```jsonc
{
  "devices": [
    {
      "id": "AA:BB:CC:DD:EE:FF",
      "name": "HYDRAO_SHOWER",
      "live": {
        "volume": 7,
        "flow": 8.4,
        "temperature": 39.1,
        "date": "2024-01-20T07:33:51",
      },
    },
  ],
}
```

The full OpenAPI specification is available in [openapi.yaml](openapi.yaml).

## WiFi Provisioning

The gateway advertises a BLE GATT server named **`HYDRAO-Setup`** at all times. The Flutter app uses it to configure the Raspberry Pi's WiFi without needing a keyboard or screen:

1. App writes the SSID to `WIFI_SSID_CHAR_UUID`
2. App writes the password to `WIFI_PWD_CHAR_UUID`
3. App writes `0x01` to `WIFI_CMD_CHAR_UUID` to trigger the connection attempt
4. App subscribes to `WIFI_STATUS_CHAR_UUID` for status notifications:
   - `idle` — waiting for credentials
   - `connecting` — connection attempt in progress
   - `ok:<ip>` — connected; IP address is included
   - `err:<reason>` — connection failed

WiFi configuration is applied via `nmcli`. Requires NetworkManager and the `bluetooth` group on the process user.

## Configuration

All tuneable constants live in [config.py](config.py):

| Constant              | Default      | Description                                       |
| --------------------- | ------------ | ------------------------------------------------- |
| `API_PORT`            | `8080`       | HTTP API port                                     |
| `SCAN_TIMEOUT`        | `20 s`       | BLE scan window duration                          |
| `CONNECT_TIMEOUT`     | `5 s`        | BLE connection timeout                            |
| `LIVE_POLL_INTERVAL`  | `1 s`        | How often live characteristics are read           |
| `MIN_VALID_VOLUME`    | `3 L`        | Showers below this volume are ignored             |
| `DEFAULT_CALIBRATION` | `545`        | Flow showerhead calibration factor                |
| `MAX_SOAPING_TIME`    | `180 s`      | Cap on reported soaping time                      |
| `DEFAULT_THRESHOLDS`  | 5/10/15/20 L | LED color thresholds when the device reports none |

## Development

The project uses [uv](https://github.com/astral-sh/uv) for dependency management and [Bruno](https://www.usebruno.com/) for API testing (see [bruno/](bruno/)).

```bash
# Install dependencies
uv sync

# Run locally (macOS — uses CoreBluetooth UUIDs instead of MAC addresses)
uv run python main.py --port 9090 --debug
```

API collections for Bruno are in [bruno/devices/](bruno/devices/). Point the `Local` environment at `http://localhost:9090`.

## Dependencies

| Package                                                        | Purpose                                            |
| -------------------------------------------------------------- | -------------------------------------------------- |
| [bleak](https://github.com/hbldh/bleak)                        | BLE central (scanner + GATT client)                |
| [bless](https://github.com/kevincar/bless)                     | BLE peripheral (GATT server for WiFi provisioning) |
| [fastapi](https://fastapi.tiangolo.com/)                       | REST API framework                                 |
| [uvicorn](https://www.uvicorn.org/)                            | ASGI server (shares the asyncio event loop)        |
| [aiosqlite](https://github.com/omnilib/aiosqlite)              | Async SQLite                                       |
| [zeroconf](https://github.com/python-zeroconf/python-zeroconf) | mDNS advertisement                                 |

## Contributing

Contributions are welcome! Here's how to get started:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -m 'Add my feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request

Please open an issue first to discuss significant changes or new features.

## License

Copyright 2026 Hydrao

Licensed under the [Apache License, Version 2.0](LICENSE). See also the [NOTICE](NOTICE) file for attribution requirements.
