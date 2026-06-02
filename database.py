import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from config import DB_PATH, DEFAULT_CALIBRATION
from models import Device, Shower, Threshold

logger = logging.getLogger(__name__)

_CREATE_DEVICES = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    uuid TEXT,
    name TEXT NOT NULL DEFAULT 'Shower',
    hw_version INTEGER,
    fw_version TEXT,
    calibration INTEGER DEFAULT 545,
    threshold TEXT,
    first_seen TIMESTAMP,
    last_seen TIMESTAMP,
    last_rssi INTEGER,
    last_sync_min_index INTEGER,
    last_sync_max_index INTEGER,
    last_sync_date TIMESTAMP,
    is_last_sync_complete BOOLEAN DEFAULT FALSE,
    index_cycle_count INTEGER DEFAULT 0,
    live_volume INTEGER DEFAULT 0,
    live_flow REAL,
    live_duration REAL,
    live_temperature REAL,
    live_date TIMESTAMP
)
"""

_CREATE_SHOWERS = """
CREATE TABLE IF NOT EXISTS showers (
    id INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    volume INTEGER NOT NULL,
    temperature REAL,
    flow REAL,
    duration REAL,
    soaping_time INTEGER,
    date TIMESTAMP NOT NULL,
    threshold TEXT,
    is_empty BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (id, device_id),
    FOREIGN KEY (device_id) REFERENCES devices(id)
)
"""

_CREATE_ALLOWED_DEVICES = """
CREATE TABLE IF NOT EXISTS allowed_devices (
    address TEXT PRIMARY KEY
)
"""


_LIVE_COLUMNS = [
    ("live_volume",      "INTEGER DEFAULT 0"),
    ("live_flow",        "REAL"),
    ("live_duration",    "REAL"),
    ("live_temperature", "REAL"),
    ("live_date",        "TIMESTAMP"),
]


async def init_db(db_path: str = DB_PATH) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute(_CREATE_DEVICES)
    await conn.execute(_CREATE_SHOWERS)
    await conn.execute(_CREATE_ALLOWED_DEVICES)
    # Migrate existing tables that pre-date the live columns.
    for col_name, col_def in _LIVE_COLUMNS:
        try:
            await conn.execute(f"ALTER TABLE devices ADD COLUMN {col_name} {col_def}")
        except aiosqlite.OperationalError:
            pass  # column already exists
    await conn.commit()
    logger.info("Database initialised at %s", db_path)
    return conn


def _thresholds_to_json(thresholds: Optional[list]) -> Optional[str]:
    if thresholds is None:
        return None
    if thresholds and hasattr(thresholds[0], "color"):
        return json.dumps([{"color": t.color, "liter": t.liter} for t in thresholds])
    return json.dumps(thresholds)


def _json_to_thresholds(text: Optional[str]) -> Optional[list[Threshold]]:
    if not text:
        return None
    return [Threshold(color=d["color"], liter=d["liter"]) for d in json.loads(text)]


async def get_device(conn: aiosqlite.Connection, device_id: str) -> Optional[Device]:
    async with conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return Device(
        id=row["id"],
        uuid=row["uuid"],
        name=row["name"],
        hw_version=row["hw_version"],
        fw_version=row["fw_version"],
        calibration=row["calibration"] or DEFAULT_CALIBRATION,
        threshold=_json_to_thresholds(row["threshold"]),
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        last_rssi=row["last_rssi"],
        last_sync_min_index=row["last_sync_min_index"],
        last_sync_max_index=row["last_sync_max_index"],
        last_sync_date=row["last_sync_date"],
        is_last_sync_complete=bool(row["is_last_sync_complete"]),
        index_cycle_count=row["index_cycle_count"] or 0,
        live_volume=row["live_volume"] or 0,
        live_flow=row["live_flow"],
        live_duration=row["live_duration"],
        live_temperature=row["live_temperature"],
        live_date=row["live_date"],
    )


async def upsert_device(conn: aiosqlite.Connection, device: Device) -> None:
    await conn.execute(
        """
        INSERT INTO devices (
            id, uuid, name, hw_version, fw_version, calibration, threshold,
            first_seen, last_seen, last_rssi, last_sync_min_index, last_sync_max_index,
            last_sync_date, is_last_sync_complete, index_cycle_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            uuid                  = excluded.uuid,
            name                  = excluded.name,
            hw_version            = excluded.hw_version,
            fw_version            = excluded.fw_version,
            calibration           = excluded.calibration,
            threshold             = excluded.threshold,
            last_seen             = excluded.last_seen,
            last_rssi             = excluded.last_rssi,
            last_sync_min_index   = excluded.last_sync_min_index,
            last_sync_max_index   = excluded.last_sync_max_index,
            last_sync_date        = excluded.last_sync_date,
            is_last_sync_complete = excluded.is_last_sync_complete,
            index_cycle_count     = excluded.index_cycle_count
        """,
        (
            device.id,
            device.uuid,
            device.name,
            device.hw_version,
            device.fw_version,
            device.calibration,
            _thresholds_to_json(device.threshold),
            device.first_seen,
            device.last_seen,
            device.last_rssi,
            device.last_sync_min_index,
            device.last_sync_max_index,
            device.last_sync_date,
            device.is_last_sync_complete,
            device.index_cycle_count,
        ),
    )
    await conn.commit()


async def insert_shower(conn: aiosqlite.Connection, shower: Shower) -> None:
    threshold_json = json.dumps(shower.threshold) if shower.threshold else None
    await conn.execute(
        """
        INSERT OR IGNORE INTO showers
            (id, device_id, volume, temperature, flow, duration, soaping_time, date, threshold, is_empty)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            shower.id,
            shower.device_id,
            shower.volume,
            shower.temperature,
            shower.flow,
            shower.duration,
            shower.soaping_time,
            shower.date,
            threshold_json,
            shower.is_empty,
        ),
    )
    await conn.commit()


async def get_max_shower_id(conn: aiosqlite.Connection, device_id: str) -> Optional[int]:
    async with conn.execute(
        "SELECT MAX(id) FROM showers WHERE device_id = ?", (device_id,)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row and row[0] is not None else None


async def update_device_live(
    conn: aiosqlite.Connection,
    device_id: str,
    *,
    volume: int = 0,
    flow: Optional[float] = None,
    duration: Optional[float] = None,
    temperature: Optional[float] = None,
    commit: bool = True,
) -> None:
    """Persist the current live-shower state to the devices table.

    Call with volume=0 and all other fields None to clear after a shower ends.
    Pass commit=False during the high-frequency live loop to avoid per-second
    fsyncs on the SD card; the caller is then responsible for periodic commits.
    """
    live_date = datetime.now(timezone.utc) if volume > 0 else None
    await conn.execute(
        """
        UPDATE devices
        SET live_volume      = ?,
            live_flow        = ?,
            live_duration    = ?,
            live_temperature = ?,
            live_date        = ?
        WHERE id = ?
        """,
        (volume, flow, duration, temperature, live_date, device_id),
    )
    if commit:
        await conn.commit()


async def get_all_devices(conn: aiosqlite.Connection) -> list:
    async with conn.execute("SELECT * FROM devices ORDER BY last_seen DESC") as cur:
        return await cur.fetchall()


async def get_showers_for_device(
    conn: aiosqlite.Connection,
    device_id: str,
    limit: int = 50,
    offset: int = 0,
) -> Optional[list]:
    device = await get_device(conn, device_id)
    if device is None:
        return None
    async with conn.execute(
        "SELECT * FROM showers WHERE device_id = ? ORDER BY date DESC LIMIT ? OFFSET ?",
        (device_id, limit, offset),
    ) as cur:
        return await cur.fetchall()


async def get_allowed_addresses(conn: aiosqlite.Connection) -> set[str]:
    async with conn.execute("SELECT address FROM allowed_devices") as cur:
        rows = await cur.fetchall()
    return {row[0] for row in rows}


async def set_allowed_addresses(conn: aiosqlite.Connection, addresses: set[str]) -> None:
    await conn.execute("DELETE FROM allowed_devices")
    await conn.executemany(
        "INSERT OR IGNORE INTO allowed_devices (address) VALUES (?)",
        [(addr,) for addr in addresses],
    )
    await conn.commit()
