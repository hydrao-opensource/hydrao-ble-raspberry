import struct
from typing import Optional

from config import DEFAULT_CALIBRATION, MAX_SOAPING_TIME, MIN_VALID_VOLUME
from models import Threshold


def decode_device_uuid(data: bytes) -> str:
    if len(data) >= 16:
        part1 = struct.unpack_from("<I", data, 0)[0]
        part2 = struct.unpack_from("<H", data, 4)[0]
        part3 = struct.unpack_from("<H", data, 6)[0]
        part4 = struct.unpack_from("<H", data, 8)[0]
        part5 = data[10:16].hex().upper()
        return f"{part1:08X}-{part2:04X}-{part3:04X}-{part4:04X}-{part5}"
    elif len(data) >= 12:
        part1 = struct.unpack_from("<I", data, 0)[0]
        part2 = struct.unpack_from("<I", data, 4)[0]
        part3 = struct.unpack_from("<I", data, 8)[0]
        return f"{part1:08X}-{part2:08X}-{part3:08X}"
    return data.hex().upper()


def decode_thresholds(data: bytes) -> list[Threshold]:
    if len(data) < 16:
        return []
    thresholds = []
    for i in range(4):
        offset = i * 4
        liters = data[offset]
        r, g, b = data[offset + 1], data[offset + 2], data[offset + 3]
        thresholds.append(Threshold(color=f"#{r:02X}{g:02X}{b:02X}", liter=float(liters)))
    return thresholds


def encode_thresholds(thresholds: list[dict]) -> bytes:
    result = bytearray(16)
    for i, t in enumerate(thresholds[:4]):
        offset = i * 4
        result[offset] = int(t["liter"])
        color = t["color"].lstrip("#")
        result[offset + 1] = int(color[0:2], 16)
        result[offset + 2] = int(color[2:4], 16)
        result[offset + 3] = int(color[4:6], 16)
    return bytes(result)


def thresholds_are_empty(thresholds: list[Threshold]) -> bool:
    return all(t.liter == 0 for t in thresholds)


def decode_shower_range(data: bytes) -> tuple[int, int]:
    if len(data) < 4:
        return 0, 0
    min_id = struct.unpack_from("<H", data, 0)[0]
    max_id = struct.unpack_from("<H", data, 2)[0]
    return min_id, max_id


def decode_calibration(data: bytes) -> int:
    if len(data) < 2:
        return DEFAULT_CALIBRATION
    return struct.unpack_from("<H", data, 0)[0]


def encode_calibration(value: int) -> bytes:
    return struct.pack("<H", value)


def decode_live_volume(data: bytes) -> int:
    if len(data) < 3:
        return 0
    return data[2]


def _convert_flow(raw: int, calibration: int) -> Optional[float]:
    if raw == 0 or calibration == 0:
        return None
    return (1000 * 60 * 20) / (calibration * raw)


def decode_live_flow(data: bytes, calibration: int) -> tuple[Optional[float], Optional[float]]:
    if len(data) < 4:
        return None, None
    instant_raw = struct.unpack_from("<H", data, 0)[0]
    average_raw = struct.unpack_from("<H", data, 2)[0]
    return _convert_flow(instant_raw, calibration), _convert_flow(average_raw, calibration)


def _convert_temperature(raw: int) -> Optional[float]:
    if raw > 3000:
        return None
    elif raw > 100:
        return -0.02635 * (raw * 16) + 79.48293
    else:
        return raw / 2


def decode_live_temperature(data: bytes) -> tuple[Optional[float], Optional[float]]:
    if len(data) < 4:
        return None, None
    instant_raw = struct.unpack_from("<H", data, 0)[0]
    average_raw = struct.unpack_from("<H", data, 2)[0]
    return _convert_temperature(instant_raw), _convert_temperature(average_raw)


def encode_shower_request(shower_id: int) -> bytes:
    return struct.pack("<H", shower_id)


def decode_shower_data(data: bytes, calibration: int) -> Optional[dict]:
    if len(data) < 7:
        return None

    # All bytes after the first → unreadable shower marker
    if all(b == 0xFF for b in data[1:]):
        return None

    shower_id = struct.unpack_from("<H", data, 0)[0]
    volume = struct.unpack_from("<H", data, 2)[0]

    if volume == 0 or volume == 65535 or volume < MIN_VALID_VOLUME:
        return None

    temp_raw = data[4]
    flow_raw = data[5]
    soaping_time = min(data[6], MAX_SOAPING_TIME)

    temperature = None if temp_raw == 0xFF else _convert_temperature(temp_raw)

    flow = None
    duration = None
    if flow_raw > 0:
        actual_raw_flow = flow_raw * 4
        flow = _convert_flow(actual_raw_flow, calibration)
        if flow:
            duration = (volume / flow) * 60

    return {
        "id": shower_id,
        "volume": volume,
        "temperature": temperature,
        "flow": flow,
        "duration": duration,
        "soaping_time": soaping_time,
    }
