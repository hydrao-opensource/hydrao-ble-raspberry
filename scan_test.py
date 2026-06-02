import asyncio
from bleak import BleakScanner


async def scan():
    print("Scanning 15s...")
    devs = await BleakScanner.discover(timeout=15, return_adv=True)
    print(f"{len(devs)} device(s) found:")
    for d, adv in devs.values():
        print(f"  {d.address}  name={d.name!r}  local_name={adv.local_name!r}")


asyncio.run(scan())
