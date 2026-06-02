import argparse
import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

import aiosqlite
import uvicorn
from bleak.backends.device import BLEDevice

import database as db
from api import app as fastapi_app
from config import API_PORT
from device import DeviceHandler
from mdns import start_mdns, stop_mdns
from scanner import scan_loop
from wifi_provision import WifiProvisionService

LOG_FILE = Path(__file__).parent / "hydrao.log"
LOG_MAX_BYTES = 2 * 1024 * 1024   # 2 MB
LOG_BACKUP_COUNT = 6               # 6 backups + 1 active = 7 files max


def _setup_logging(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.INFO
    fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"

    root = logging.getLogger()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(fmt))
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    )
    file_handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(file_handler)


logger = logging.getLogger(__name__)


async def run(port: int) -> None:
    conn = await db.init_db()
    stop_event = asyncio.Event()
    device_tasks: dict[str, asyncio.Task] = {}

    # Load the allowed-devices list from DB; shared set mutated in-place by the API.
    allowed_addresses: set[str] = await db.get_allowed_addresses(conn)

    # Wire shared state into the FastAPI app.
    fastapi_app.state.db_conn = conn
    fastapi_app.state.allowed_addresses = allowed_addresses

    def on_device_found(ble_device: BLEDevice, rssi: int, adv_name: str) -> None:
        addr = ble_device.address
        if addr in device_tasks and not device_tasks[addr].done():
            return  # already managing this device
        # Purge finished tasks so the dict doesn't grow indefinitely.
        stale = [a for a, t in device_tasks.items() if t.done()]
        for a in stale:
            del device_tasks[a]
        logger.info("Spawning handler for %s (%s)", addr, adv_name)
        handler = DeviceHandler(ble_device, rssi, conn, adv_name=adv_name)
        task = asyncio.create_task(handler.run(), name=f"device-{addr}")
        device_tasks[addr] = task

    def _shutdown(*_) -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    # Start the HTTP API server (shares the asyncio event loop — non-blocking).
    uvicorn_config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        loop="none",  # reuse the running loop
    )
    server = uvicorn.Server(uvicorn_config)
    api_task = asyncio.create_task(server.serve(), name="api-server")

    # Start WiFi provisioning BLE GATT server (always-on for first-use and reconfiguration).
    wifi_provision = WifiProvisionService()
    wifi_task = asyncio.create_task(wifi_provision.start(), name="wifi-provision")

    # Advertise the service on the local network so Flutter can find it.
    zc = await start_mdns(port)

    scanner_task = asyncio.create_task(
        scan_loop(on_device_found, stop_event, allowed_addresses=allowed_addresses),
        name="scanner",
    )

    logger.info("HYDRAO collector started (API on port %d)", port)
    await stop_event.wait()

    logger.info("Stopping WiFi provisioning service...")
    await wifi_provision.stop()
    wifi_task.cancel()
    try:
        await wifi_task
    except asyncio.CancelledError:
        pass

    logger.info("Stopping scanner...")
    scanner_task.cancel()
    try:
        await scanner_task
    except asyncio.CancelledError:
        pass

    if device_tasks:
        logger.info("Cancelling %d device task(s)...", len(device_tasks))
        for task in device_tasks.values():
            task.cancel()
        await asyncio.gather(*device_tasks.values(), return_exceptions=True)

    logger.info("Stopping API server...")
    server.should_exit = True
    await api_task

    logger.info("Stopping mDNS...")
    await stop_mdns(zc)

    await conn.close()
    logger.info("Shutdown complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="HYDRAO BLE collector")
    parser.add_argument("--port", type=int, default=API_PORT, help="FastAPI port (default: %(default)s)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    _setup_logging(debug=args.debug)
    asyncio.run(run(args.port))


if __name__ == "__main__":
    main()
