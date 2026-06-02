import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from bleak import BleakClient, BleakError
from bleak.backends.device import BLEDevice

import database as db
import protocol as proto
from config import (
    CHAR,
    CONNECT_TIMEOUT,
    DEFAULT_CALIBRATION,
    DEFAULT_THRESHOLDS,
    LEGACY_FLOW,
    LIVE_POLL_INTERVAL,
    LIVE_SHOWER_ID_OFFSET,
)
from models import Device, LiveState, Shower, Threshold

logger = logging.getLogger(__name__)


def _detect_type(name: str) -> str:
    name_lower = name.lower()
    for t in ("aloe", "cereus", "yucca", "first", "mixer"):
        if t in name_lower:
            return t
    return "unknown"


class DeviceHandler:
    """Manages the full lifecycle of one HYDRAO BLE device: connect → sync → monitor."""

    def __init__(
        self,
        ble_device: BLEDevice,
        rssi: int,
        db_conn: aiosqlite.Connection,
        adv_name: str = "",
    ):
        self.ble_device = ble_device
        self.address = ble_device.address
        self.rssi = rssi
        self.db_conn = db_conn
        # Prefer the advertisement name (local_name) over the OS-cached display
        # name, which may differ (e.g. 'Hydrao' vs 'HYDRAO_SHOWER_ALOE_…').
        self.adv_name = adv_name or ble_device.name or "Shower"

        self.device_record: Optional[Device] = None
        self.live_state = LiveState()

        self._stop = asyncio.Event()
        self._shower_notif_event = asyncio.Event()
        self._shower_notif_data: Optional[bytes] = None

    # ------------------------------------------------------------------ public

    async def run(self) -> None:
        """Single connection attempt; the scanner re-spawns this handler on rediscovery."""
        try:
            await self._connect_and_run()
        except (BleakError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("[%s] Connection error: %s", self.address, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] Unexpected error", self.address)

    def stop(self) -> None:
        self._stop.set()

    # ----------------------------------------------------------------- connect

    async def _connect_and_run(self) -> None:
        logger.info("[%s] Connecting...", self.address)
        async with BleakClient(
            self.ble_device,
            timeout=CONNECT_TIMEOUT,
            disconnected_callback=lambda _: logger.warning("[%s] Disconnected", self.address),
        ) as client:
            logger.info("[%s] Connected", self.address)
            await self._on_connected(client)

    async def _on_connected(self, client: BleakClient) -> None:
        self.device_record = await db.get_device(self.db_conn, self.address)
        if self.device_record is None:
            await self._setup_new_device(client)
        else:
            await self._sync_known_device(client)
        # Capture a first live snapshot before the history sync, mirroring the
        # Flutter pattern where _updateShLive is called before _syncMissingShowers.
        # This ensures at least one DB write even when a long sync causes a disconnect.
        await self._read_and_persist_live(client)
        await self._sync_history(client)
        await self._live_monitor_loop(client)

    # ---------------------------------------------------------- device setup

    async def _setup_new_device(self, client: BleakClient) -> None:
        logger.info("[%s] First connection — reading device metadata", self.address)
        now = datetime.now(timezone.utc)

        uuid_data  = await self._read(client, "uuid")
        hw_data    = await self._read(client, "hw_version")
        fw_data    = await self._read(client, "fw_version")
        thresh_data = await self._read(client, "thresholds")
        cal_data   = await self._read(client, "calibration")

        thresholds = proto.decode_thresholds(thresh_data) if thresh_data else []
        if proto.thresholds_are_empty(thresholds):
            logger.info("[%s] Writing default thresholds", self.address)
            await self._write(client, "thresholds", proto.encode_thresholds(DEFAULT_THRESHOLDS))
            thresholds = [Threshold(**t) for t in DEFAULT_THRESHOLDS]

        self.device_record = Device(
            id=self.address,
            uuid=proto.decode_device_uuid(uuid_data) if uuid_data else None,
            name=self.adv_name,
            type=_detect_type(self.adv_name),
            hw_version=hw_data[0] if hw_data else None,
            fw_version=fw_data.rstrip(b"\x00").decode("utf-8", errors="ignore") if fw_data else None,
            calibration=proto.decode_calibration(cal_data) if cal_data else DEFAULT_CALIBRATION,
            threshold=thresholds,
            first_seen=now,
            last_seen=now,
            last_rssi=self.rssi,
        )
        await db.upsert_device(self.db_conn, self.device_record)
        logger.info("[%s] Registered new device: %s", self.address, self.device_record.uuid)

    async def _sync_known_device(self, client: BleakClient) -> None:
        logger.info("[%s] Refreshing device metadata", self.address)
        d = self.device_record

        uuid_data   = await self._read(client, "uuid")
        hw_data     = await self._read(client, "hw_version")
        fw_data     = await self._read(client, "fw_version")
        thresh_data = await self._read(client, "thresholds")
        cal_data    = await self._read(client, "calibration")

        if uuid_data:
            d.uuid = proto.decode_device_uuid(uuid_data)
        if hw_data:
            d.hw_version = hw_data[0]
        if fw_data:
            d.fw_version = fw_data.rstrip(b"\x00").decode("utf-8", errors="ignore")
        if thresh_data:
            d.threshold = proto.decode_thresholds(thresh_data)
        if cal_data:
            d.calibration = proto.decode_calibration(cal_data)

        d.last_seen = datetime.now(timezone.utc)
        d.last_rssi = self.rssi
        await db.upsert_device(self.db_conn, d)

    # ---------------------------------------------------------- history sync

    # How often to persist sync progress so a mid-sync interruption resumes
    # from a recent checkpoint rather than from scratch.
    _PROGRESS_SAVE_INTERVAL = 5

    async def _sync_history(self, client: BleakClient) -> None:
        range_data = await self._read(client, "shower_range")
        if not range_data:
            return

        device_min, device_max = proto.decode_shower_range(range_data)
        logger.info("[%s] Device shower range: %d–%d", self.address, device_min, device_max)

        if device_max == 0:
            logger.info("[%s] No showers stored on device", self.address)
            return

        d = self.device_record
        stored_max = d.last_sync_max_index

        if stored_max is None:
            ids_to_fetch = list(range(device_min, device_max + 1))
        elif device_max < stored_max:
            # Index wrapped around past 65535
            logger.info("[%s] Shower index wraparound detected", self.address)
            d.index_cycle_count += 1
            ids_to_fetch = list(range(0, device_max + 1))
        else:
            ids_to_fetch = list(range(stored_max + 1, device_max + 1))

        if not ids_to_fetch:
            logger.info("[%s] No new showers to sync", self.address)
            d.is_last_sync_complete = True
            await db.upsert_device(self.db_conn, d)
            return

        total = len(ids_to_fetch)
        logger.info("[%s] Fetching %d shower(s) [%d–%d]", self.address, total, ids_to_fetch[0], ids_to_fetch[-1])

        # Subscribe to notifications before issuing any request to avoid races.
        # Fall back to polling (direct reads) if notifications are unsupported.
        use_notify = False
        try:
            await client.start_notify(CHAR["shower_data"], self._on_shower_notification)
            await asyncio.sleep(0.2)  # let the CCCD write settle on the peripheral
            use_notify = True
            logger.debug("[%s] Shower notifications subscribed", self.address)
        except Exception as exc:
            logger.warning("[%s] Cannot subscribe to notifications (%s) — falling back to polling", self.address, exc)

        saved = skipped = 0
        completed = False
        try:
            for i, shower_id in enumerate(ids_to_fetch):
                if self._stop.is_set() or not client.is_connected:
                    logger.info("[%s] Sync interrupted at shower %d (%d/%d)", self.address, shower_id, i, total)
                    break

                if await self._fetch_one_shower(client, shower_id, use_notify=use_notify):
                    saved += 1
                else:
                    skipped += 1

                # Persist progress periodically so a reconnect can resume here.
                if (i + 1) % self._PROGRESS_SAVE_INTERVAL == 0:
                    d.last_sync_min_index = device_min
                    d.last_sync_max_index = shower_id
                    d.last_sync_date = datetime.now(timezone.utc)
                    d.is_last_sync_complete = False
                    await db.upsert_device(self.db_conn, d)
                    logger.info("[%s] Progress: %d/%d  saved=%d  skipped=%d", self.address, i + 1, total, saved, skipped)
            else:
                # for/else: loop ran to completion without a break
                completed = True
        finally:
            if use_notify:
                try:
                    await client.stop_notify(CHAR["shower_data"])
                except Exception:
                    pass

        if completed:
            d.last_sync_min_index = device_min
            d.last_sync_max_index = device_max
            d.last_sync_date = datetime.now(timezone.utc)
            d.is_last_sync_complete = True
            await db.upsert_device(self.db_conn, d)
            logger.info("[%s] History sync complete — saved=%d  skipped=%d", self.address, saved, skipped)
        else:
            logger.info("[%s] Sync partial — saved=%d  skipped=%d  will resume on next connect", self.address, saved, skipped)

    def _on_shower_notification(self, _sender: int, data: bytearray) -> None:
        self._shower_notif_data = bytes(data)
        self._shower_notif_event.set()

    async def _fetch_one_shower(
        self, client: BleakClient, shower_id: int, *, use_notify: bool = True
    ) -> bool:
        """Request one shower from the device and save it. Returns True if saved."""
        self._shower_notif_event.clear()
        self._shower_notif_data = None

        await self._write(client, "shower_request", proto.encode_shower_request(shower_id))

        data: Optional[bytes] = None

        if use_notify:
            try:
                await asyncio.wait_for(self._shower_notif_event.wait(), timeout=3.0)
                data = self._shower_notif_data
            except asyncio.TimeoutError:
                # Notification didn't arrive — try a direct read as fallback.
                logger.debug("[%s] Notification timeout for shower %d, trying direct read", self.address, shower_id)
                await asyncio.sleep(0.1)
                data = await self._read(client, "shower_data")
        else:
            # Polling mode: give the device a moment then read directly.
            await asyncio.sleep(0.2)
            data = await self._read(client, "shower_data")

        if data is None:
            logger.debug("[%s] No data received for shower %d", self.address, shower_id)
            return False

        decoded = proto.decode_shower_data(data, self.device_record.calibration)
        if decoded is None:
            logger.debug("[%s] Shower %d invalid/empty — skipped", self.address, shower_id)
            return False

        # Offset the stored ID by the wraparound cycle count so that a shower
        # with device-side id=5 on cycle 1 (65536+5) doesn't collide in the DB
        # with id=5 on cycle 0.  Cycle 0 produces no offset (backwards-compatible).
        stored_id = decoded["id"] + self.device_record.index_cycle_count * 65536

        shower = Shower(
            id=stored_id,
            device_id=self.address,
            volume=decoded["volume"],
            temperature=decoded["temperature"],
            flow=decoded["flow"],
            duration=decoded["duration"],
            soaping_time=decoded["soaping_time"],
            date=datetime.now(timezone.utc),
            threshold=self._current_threshold_json(),
        )
        await db.insert_shower(self.db_conn, shower)
        logger.info(
            "[%s] Saved shower id=%d  vol=%dL  temp=%s  flow=%s  soaping=%ds",
            self.address,
            shower.id,
            shower.volume,
            f"{shower.temperature:.1f}°C" if shower.temperature is not None else "N/A",
            f"{shower.flow:.1f}L/min" if shower.flow is not None else "N/A",
            shower.soaping_time or 0,
        )
        return True

    # ---------------------------------------------------------- live monitor

    async def _read_and_persist_live(self, client: BleakClient) -> None:
        """Read a single live snapshot and persist it to the DB."""
        if not client.is_connected:
            return
        d = self.device_record
        calibration = d.calibration if d else DEFAULT_CALIBRATION
        legacy = d.hw_version is not None and d.hw_version < 8

        vol_data = await self._read(client, "live_volume")
        if vol_data is None:
            return
        volume = proto.decode_live_volume(vol_data)

        if volume > 0:
            flow_data = await self._read(client, "live_flow")
            temp_data = await self._read(client, "live_temp")
            instant_flow, _ = proto.decode_live_flow(flow_data, calibration) if flow_data else (None, None)
            instant_temp, _ = proto.decode_live_temperature(temp_data) if temp_data else (None, None)
            if legacy:
                instant_flow = LEGACY_FLOW
            live_duration = (volume / instant_flow * 60) if instant_flow else None
            await db.update_device_live(
                self.db_conn, self.address,
                volume=volume, flow=instant_flow, duration=live_duration, temperature=instant_temp,
            )
            logger.info("[%s] Initial live snapshot: vol=%dL  flow=%s  temp=%s",
                self.address, volume,
                f"{instant_flow:.1f}L/min" if instant_flow is not None else "N/A",
                f"{instant_temp:.1f}°C" if instant_temp is not None else "N/A",
            )
        else:
            logger.debug("[%s] Initial live snapshot: no active shower (vol=0)", self.address)

    async def _live_monitor_loop(self, client: BleakClient) -> None:
        if not client.is_connected:
            logger.warning("[%s] Live monitor skipped — client already disconnected", self.address)
            return
        logger.info("[%s] Starting live monitor", self.address)
        d = self.device_record
        calibration = d.calibration if d else DEFAULT_CALIBRATION
        legacy = d.hw_version is not None and d.hw_version < 8

        shower_active = False
        session_volume = 0
        session_flow_samples: list[float] = []
        session_temp_samples: list[float] = []
        session_start: Optional[datetime] = None
        # Commit live writes to disk at most every N polls instead of every second
        # to reduce SD-card wear.  Data in the open transaction is still readable
        # by the WebSocket (same connection).
        _LIVE_COMMIT_INTERVAL = 10
        _polls_since_commit = 0

        while not self._stop.is_set() and client.is_connected:
            vol_data = await self._read(client, "live_volume")
            if vol_data is None:
                await asyncio.sleep(LIVE_POLL_INTERVAL)
                continue

            volume = proto.decode_live_volume(vol_data)

            if volume > 0:
                flow_data = await self._read(client, "live_flow")
                temp_data = await self._read(client, "live_temp")

                instant_flow, avg_flow = (
                    proto.decode_live_flow(flow_data, calibration) if flow_data else (None, None)
                )
                instant_temp, avg_temp = (
                    proto.decode_live_temperature(temp_data) if temp_data else (None, None)
                )

                if legacy:
                    instant_flow = LEGACY_FLOW
                    avg_flow = LEGACY_FLOW

                self.live_state = LiveState(
                    volume=volume,
                    instant_flow=instant_flow,
                    average_flow=avg_flow,
                    instant_temp=instant_temp,
                    average_temp=avg_temp,
                    is_showering=True,
                )

                if not shower_active:
                    shower_active = True
                    session_start = datetime.now(timezone.utc)
                    session_flow_samples = []
                    session_temp_samples = []
                    logger.info("[%s] Shower started", self.address)

                session_volume = volume
                if instant_flow is not None:
                    session_flow_samples.append(instant_flow)
                if instant_temp is not None:
                    session_temp_samples.append(instant_temp)

                live_duration = (volume / instant_flow * 60) if instant_flow else None
                await db.update_device_live(
                    self.db_conn,
                    self.address,
                    volume=volume,
                    flow=instant_flow,
                    duration=live_duration,
                    temperature=instant_temp,
                    commit=False,
                )
                _polls_since_commit += 1
                if _polls_since_commit >= _LIVE_COMMIT_INTERVAL:
                    await self.db_conn.commit()
                    _polls_since_commit = 0

                logger.debug(
                    "[%s] Live: vol=%dL  flow=%s  temp=%s",
                    self.address,
                    volume,
                    f"{instant_flow:.1f}L/min" if instant_flow is not None else "N/A",
                    f"{instant_temp:.1f}°C" if instant_temp is not None else "N/A",
                )

            elif shower_active:
                shower_active = False
                self.live_state = LiveState()
                logger.info("[%s] Shower ended: vol=%dL", self.address, session_volume)

                avg_flow_val = (
                    sum(session_flow_samples) / len(session_flow_samples)
                    if session_flow_samples
                    else None
                )
                avg_temp_val = (
                    sum(session_temp_samples) / len(session_temp_samples)
                    if session_temp_samples
                    else None
                )
                duration = (session_volume / avg_flow_val * 60) if avg_flow_val else None

                await db.update_device_live(self.db_conn, self.address)  # commit=True: flushes the clear
                _polls_since_commit = 0

                # Use a synthetic ID above the uint16 device range to avoid
                # colliding with historically-synced shower IDs (0–65535).
                current_max = await db.get_max_shower_id(self.db_conn, self.address)
                live_id = max(
                    LIVE_SHOWER_ID_OFFSET,
                    (current_max + 1) if current_max is not None else LIVE_SHOWER_ID_OFFSET,
                )

                shower = Shower(
                    id=live_id,
                    device_id=self.address,
                    volume=session_volume,
                    temperature=avg_temp_val,
                    flow=avg_flow_val,
                    duration=duration,
                    soaping_time=None,
                    date=session_start or datetime.now(timezone.utc),
                    threshold=self._current_threshold_json(),
                )
                await db.insert_shower(self.db_conn, shower)
                logger.info("[%s] Live shower saved (id=%d)", self.address, live_id)

            await asyncio.sleep(LIVE_POLL_INTERVAL)

    # --------------------------------------------------------------- helpers

    def _current_threshold_json(self) -> Optional[list[dict]]:
        d = self.device_record
        if d is None or not d.threshold:
            return None
        return [{"color": t.color, "liter": t.liter} for t in d.threshold]

    async def _read(self, client: BleakClient, char_name: str) -> Optional[bytes]:
        try:
            return bytes(await client.read_gatt_char(CHAR[char_name]))
        except Exception as exc:
            logger.warning("[%s] Read %s failed: %s", self.address, char_name, exc)
            return None

    async def _write(self, client: BleakClient, char_name: str, data: bytes) -> bool:
        try:
            await client.write_gatt_char(CHAR[char_name], data, response=True)
            return True
        except Exception as exc:
            logger.warning("[%s] Write %s failed: %s", self.address, char_name, exc)
            return False
