"""BLE GATT server for WiFi provisioning.

The Flutter app connects to the "HYDRAO-Setup" peripheral and:
  1. Writes the SSID  → WIFI_SSID_CHAR_UUID
  2. Writes the password → WIFI_PWD_CHAR_UUID
  3. Writes 0x01 → WIFI_CMD_CHAR_UUID  to trigger the connection attempt
  4. Subscribes to WIFI_STATUS_CHAR_UUID for progress notifications:
       b"idle"            — waiting for credentials
       b"connecting"      — nmcli in progress
       b"ok:<ip>"         — connected; <ip> is the assigned address
       b"err:<reason>"    — connection failed

Requirements on the Pi:
  - BlueZ >= 5.43 (Raspberry Pi OS Bookworm ships 5.66)
  - NetworkManager + nmcli  (default on Raspberry Pi OS Bookworm)
  - The process user must be in the `bluetooth` group

Security note: no BLE-level encryption in this initial implementation.
  Add a BlueZ pairing agent (Passkey or Numeric Comparison) for production.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from bless import (
    BlessGATTCharacteristic,
    BlessServer,
    GATTAttributePermissions,
    GATTCharacteristicProperties,
)

from config import (
    WIFI_CMD_CHAR_UUID,
    WIFI_PWD_CHAR_UUID,
    WIFI_SERVICE_UUID,
    WIFI_SSID_CHAR_UUID,
    WIFI_STATUS_CHAR_UUID,
)

logger = logging.getLogger(__name__)

_STATUS_IDLE = b"idle"
_CMD_APPLY   = 0x01


class WifiProvisionService:
    """Runs a BLE GATT peripheral that lets a Flutter app configure WiFi."""

    def __init__(self) -> None:
        self._server: Optional[BlessServer] = None
        self._ssid: str = ""
        self._password: str = ""

    # ------------------------------------------------------------------
    # Lifecycle

    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        self._server = BlessServer(name="HYDRAO-Setup", loop=loop)
        self._server.read_request_func  = self._handle_read
        self._server.write_request_func = self._handle_write

        await self._server.add_new_service(WIFI_SERVICE_UUID)

        # SSID — write only
        await self._server.add_new_characteristic(
            WIFI_SERVICE_UUID,
            WIFI_SSID_CHAR_UUID,
            GATTCharacteristicProperties.write,
            None,
            GATTAttributePermissions.writeable,
        )

        # Password — write only (never readable)
        await self._server.add_new_characteristic(
            WIFI_SERVICE_UUID,
            WIFI_PWD_CHAR_UUID,
            GATTCharacteristicProperties.write,
            None,
            GATTAttributePermissions.writeable,
        )

        # Command — write only  (0x01 = apply)
        await self._server.add_new_characteristic(
            WIFI_SERVICE_UUID,
            WIFI_CMD_CHAR_UUID,
            GATTCharacteristicProperties.write,
            None,
            GATTAttributePermissions.writeable,
        )

        # Status — read + notify
        await self._server.add_new_characteristic(
            WIFI_SERVICE_UUID,
            WIFI_STATUS_CHAR_UUID,
            GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify,
            bytearray(_STATUS_IDLE),
            GATTAttributePermissions.readable,
        )

        await self._server.start()
        logger.info("WiFi provisioning BLE service started (advertising as 'HYDRAO-Setup')")

    async def stop(self) -> None:
        if self._server:
            await self._server.stop()
            self._server = None
            logger.info("WiFi provisioning BLE service stopped")

    # ------------------------------------------------------------------
    # GATT callbacks

    def _handle_read(self, characteristic: BlessGATTCharacteristic, **_: Any) -> bytearray:
        return bytearray(characteristic.value or b"")

    def _handle_write(self, characteristic: BlessGATTCharacteristic, value: Any, **_: Any) -> None:
        data = bytearray(value)
        uuid = str(characteristic.uuid).lower()

        if uuid == WIFI_SSID_CHAR_UUID.lower():
            self._ssid = data.decode("utf-8", errors="replace").strip()
            logger.info("WiFi provision: SSID received (%d chars)", len(self._ssid))

        elif uuid == WIFI_PWD_CHAR_UUID.lower():
            self._password = data.decode("utf-8", errors="replace")
            logger.info("WiFi provision: password received (%d chars)", len(self._password))

        elif uuid == WIFI_CMD_CHAR_UUID.lower():
            if data and data[0] == _CMD_APPLY:
                asyncio.create_task(self._apply_wifi())

    # ------------------------------------------------------------------
    # WiFi application

    async def _apply_wifi(self) -> None:
        if not self._ssid:
            logger.warning("WiFi provision: APPLY received but SSID is empty")
            return

        logger.info("WiFi provision: connecting to '%s'...", self._ssid)
        self._notify_status(b"connecting")

        # Remove any stale saved connection with the same SSID so that a
        # password update is not silently ignored by NetworkManager.
        await asyncio.create_subprocess_exec(
            "nmcli", "connection", "delete", self._ssid,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        proc = await asyncio.create_subprocess_exec(
            "nmcli", "--wait", "30",
            "dev", "wifi", "connect", self._ssid,
            "password", self._password,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode == 0:
            ip = await self._wifi_ip()
            status = f"ok:{ip}".encode()
            logger.info("WiFi provision: connected to '%s' — IP %s", self._ssid, ip)
        else:
            reason = _last_line(stderr)[:80]
            status = f"err:{reason}".encode()
            logger.warning("WiFi provision: failed — %s", reason)

        self._notify_status(status)
        # Clear password from memory once the attempt is done.
        self._password = ""

    async def _wifi_ip(self) -> str:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "-g", "IP4.ADDRESS", "dev", "show", "wlan0",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        # Output is "192.168.1.42/24\n" — strip the prefix length.
        return out.decode().strip().split("/")[0] or "unknown"

    def _notify_status(self, value: bytes) -> None:
        if not self._server:
            return
        char = self._server.get_characteristic(WIFI_STATUS_CHAR_UUID)
        if char is None:
            return
        char.value = bytearray(value)
        self._server.update_value(WIFI_SERVICE_UUID, WIFI_STATUS_CHAR_UUID)


# ------------------------------------------------------------------
# Helpers

def _last_line(stderr_bytes: bytes) -> str:
    text = stderr_bytes.decode("utf-8", errors="replace").strip()
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else "unknown error"
