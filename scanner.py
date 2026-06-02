import asyncio
import logging
from typing import Callable, Optional

from bleak import BleakScanner
from bleak.backends.device import BLEDevice

from config import DEVICE_NAME_PREFIX, SCAN_IDLE_INTERVAL, SCAN_TIMEOUT

logger = logging.getLogger(__name__)


async def scan_loop(
    on_device_found: Callable[[BLEDevice, int, str], None],
    stop_event: Optional[asyncio.Event] = None,
    allowed_addresses: Optional[set[str]] = None,
) -> None:
    """Scan continuously for HYDRAO devices and call on_device_found for each one.

    If allowed_addresses is a non-empty set, only devices whose address (uppercased)
    appears in that set will trigger on_device_found.  An empty set or None means
    no filter — all HYDRAO devices are accepted (backward-compatible behaviour).
    """
    while stop_event is None or not stop_event.is_set():
        logger.info("Scanning for HYDRAO devices (timeout=%ds)...", SCAN_TIMEOUT)
        try:
            discovered = await BleakScanner.discover(
                timeout=SCAN_TIMEOUT,
                return_adv=True,
            )
            found = 0
            for ble_device, adv_data in discovered.values():
                # local_name comes directly from the BLE advertisement and is
                # more reliable than ble_device.name, which may be a cached
                # display name from the OS (e.g. 'Hydrao' on macOS instead of
                # 'HYDRAO_SHOWER_ALOE_…').
                effective_name = adv_data.local_name or ble_device.name or ""
                if not effective_name.startswith(DEVICE_NAME_PREFIX):
                    continue
                addr = ble_device.address.upper()
                if allowed_addresses and addr not in allowed_addresses:
                    logger.debug("Skipping %s (%s) — not in allowed list", addr, effective_name)
                    continue
                found += 1
                rssi = adv_data.rssi if adv_data.rssi is not None else 0
                logger.info(
                    "Discovered: %s  addr=%s  rssi=%d",
                    effective_name,
                    ble_device.address,
                    rssi,
                )
                on_device_found(ble_device, rssi, effective_name)
            if found == 0:
                logger.info("No HYDRAO devices found in this scan cycle")
            await asyncio.sleep(SCAN_IDLE_INTERVAL)
        except Exception:
            logger.exception("Error during BLE scan, retrying in 5s")
            await asyncio.sleep(5)
