from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Threshold:
    color: str
    liter: float


@dataclass
class Device:
    id: str                                    # BLE MAC address
    name: str = "Shower"
    uuid: Optional[str] = None
    hw_version: Optional[int] = None
    fw_version: Optional[str] = None
    calibration: int = 545
    threshold: Optional[list[Threshold]] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    last_rssi: Optional[int] = None
    last_sync_min_index: Optional[int] = None
    last_sync_max_index: Optional[int] = None
    last_sync_date: Optional[datetime] = None
    is_last_sync_complete: bool = False
    index_cycle_count: int = 0
    live_volume: int = 0
    live_flow: Optional[float] = None
    live_duration: Optional[float] = None
    live_temperature: Optional[float] = None
    live_date: Optional[datetime] = None


@dataclass
class Shower:
    id: int                                    # shower index from device (or synthetic for live)
    device_id: str
    volume: int                                # liters
    date: datetime
    temperature: Optional[float] = None       # Celsius, None if sensor absent
    flow: Optional[float] = None              # L/min
    duration: Optional[float] = None          # seconds (computed)
    soaping_time: Optional[int] = None        # seconds (0–180)
    threshold: Optional[list[dict]] = None    # JSON snapshot at shower time
    is_empty: bool = False


@dataclass
class LiveState:
    volume: int = 0
    instant_flow: Optional[float] = None
    average_flow: Optional[float] = None
    instant_temp: Optional[float] = None
    average_temp: Optional[float] = None
    is_showering: bool = False
