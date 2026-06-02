from __future__ import annotations

import asyncio
import json
import logging
import re

import aiosqlite
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, field_validator

import database as db

logger = logging.getLogger(__name__)

app = FastAPI(title="HYDRAO BLE Collector", version="1.0.0")

_MAC_RE = re.compile(r'^([0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}$')


class AllowedDevicesPayload(BaseModel):
    addresses: list[str]

    @field_validator("addresses")
    @classmethod
    def validate_mac_addresses(cls, v: list[str]) -> list[str]:
        for addr in v:
            if not _MAC_RE.match(addr):
                raise ValueError(f"Invalid MAC address format: {addr!r}")
        return v


# ------------------------------------------------------------------ helpers

def _row_to_device(row: aiosqlite.Row) -> dict:
    return {
        "id": row["id"],
        "uuid": row["uuid"],
        "name": row["name"],
        "hw_version": row["hw_version"],
        "fw_version": row["fw_version"],
        "calibration": row["calibration"],
        "threshold": json.loads(row["threshold"]) if row["threshold"] else None,
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "last_rssi": row["last_rssi"],
        "last_sync_min_index": row["last_sync_min_index"],
        "last_sync_max_index": row["last_sync_max_index"],
        "last_sync_date": row["last_sync_date"],
        "is_last_sync_complete": bool(row["is_last_sync_complete"]),
        "index_cycle_count": row["index_cycle_count"],
        "live": {
            "volume": row["live_volume"] or 0,
            "flow": row["live_flow"],
            "duration": row["live_duration"],
            "temperature": row["live_temperature"],
            "date": row["live_date"],
        },
    }


def _row_to_shower(row: aiosqlite.Row) -> dict:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "volume": row["volume"],
        "temperature": row["temperature"],
        "flow": row["flow"],
        "duration": row["duration"],
        "soaping_time": row["soaping_time"],
        "date": row["date"],
        "threshold": json.loads(row["threshold"]) if row["threshold"] else None,
        "is_empty": bool(row["is_empty"]),
    }


# ------------------------------------------------------------------ devices

@app.get("/devices")
async def list_devices(request: Request) -> dict:
    conn: aiosqlite.Connection = request.app.state.db_conn
    allowed: set[str] = request.app.state.allowed_addresses
    rows = await db.get_all_devices(conn)
    return {
        "devices": [_row_to_device(r) for r in rows],
        "allowed_filter_active": len(allowed) > 0,
        "allowed_addresses": list(allowed),
    }


@app.put("/devices/allowed")
async def set_allowed_devices(payload: AllowedDevicesPayload, request: Request) -> dict:
    conn: aiosqlite.Connection = request.app.state.db_conn
    normalized = {addr.upper() for addr in payload.addresses}
    await db.set_allowed_addresses(conn, normalized)
    # Mutate the shared set in-place so the scanner picks up the change immediately.
    allowed: set[str] = request.app.state.allowed_addresses
    allowed.clear()
    allowed.update(normalized)
    logger.info("Allowed devices updated: %d address(es)", len(normalized))
    return {"allowed_addresses": list(normalized)}


@app.get("/devices/{device_id}/showers")
async def get_showers(
    device_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    conn: aiosqlite.Connection = request.app.state.db_conn
    rows = await db.get_showers_for_device(conn, device_id, limit=limit, offset=offset)
    if rows is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return {
        "device_id": device_id,
        "showers": [_row_to_shower(r) for r in rows],
        "limit": limit,
        "offset": offset,
    }


@app.get("/devices/{device_id}/live")
async def get_live(device_id: str, request: Request) -> dict:
    conn: aiosqlite.Connection = request.app.state.db_conn
    device = await db.get_device(conn, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return {
        "device_id": device_id,
        "live": {
            "volume": device.live_volume,
            "flow": device.live_flow,
            "duration": device.live_duration,
            "temperature": device.live_temperature,
            "date": str(device.live_date) if device.live_date else None,
        },
    }


# ------------------------------------------------------------------ websocket

@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket) -> None:
    """Push a live snapshot of all devices every 2 seconds."""
    await websocket.accept()
    conn: aiosqlite.Connection = websocket.app.state.db_conn
    try:
        while True:
            rows = await db.get_all_devices(conn)
            await websocket.send_json({
                "devices": [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "live": {
                            "volume": r["live_volume"] or 0,
                            "flow": r["live_flow"],
                            "temperature": r["live_temperature"],
                            "date": r["live_date"],
                        },
                    }
                    for r in rows
                ]
            })
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
