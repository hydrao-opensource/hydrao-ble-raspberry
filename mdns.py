from __future__ import annotations

import logging
import socket

from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

logger = logging.getLogger(__name__)

_SERVICE_TYPE = "_hydrao._tcp.local."
_SERVICE_NAME = f"HYDRAO BLE Collector.{_SERVICE_TYPE}"


def _local_ip() -> str:
    try:
        # Connect to an external address to resolve the outbound interface IP.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


async def start_mdns(port: int) -> AsyncZeroconf:
    ip = _local_ip()
    info = AsyncServiceInfo(
        _SERVICE_TYPE,
        _SERVICE_NAME,
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties={"version": "1"},
    )
    zc = AsyncZeroconf()
    await zc.async_register_service(info)
    logger.info("mDNS: advertising '%s' on %s:%d", _SERVICE_NAME, ip, port)
    return zc


async def stop_mdns(zc: AsyncZeroconf) -> None:
    await zc.async_unregister_all_services()
    await zc.async_close()
