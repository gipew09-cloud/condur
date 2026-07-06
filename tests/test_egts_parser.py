"""
Тесты парсера ЕГТС (app/telemetry/egts.py): CRC на реальном заголовке из
логов Railway, полный цикл «собрали пакет → разобрали», ответ-подтверждение,
нарезка TCP-потока. Без БД и сети.
"""
import struct
from datetime import datetime, timedelta, timezone

import pytest

from app.telemetry import egts

_EPOCH = datetime(2010, 1, 1, tzinfo=timezone.utc)

# Первые 24 байта реального пакета Stavtrack из логов Railway (id=73).
REAL_HEADER = bytes.fromhex("01 00 01 0b 00 2f 00 af f4 01 77".replace(" ", ""))
REAL_SFRD_PREFIX = bytes.fromhex("20 00 01 00 05 ec fa 01 00 3f a2 0a 1f".replace(" ", ""))


def test_crc8_matches_real_stavtrack_header():
    assert egts.crc8(REAL_HEADER[:10]) == REAL_HEADER[10] == 0x77


def test_real_header_fields():
    hl = REAL_HEADER[3]
    fdl, pid = struct.unpack_from("<HH", REAL_HEADER, 5)
    assert hl == 11 and fdl == 47 and REAL_HEADER[9] == egts.PT_APPDATA
    # OID из реального пакета = Stavtrack ID машины владельца
    oid = struct.unpack_from("<I", REAL_SFRD_PREFIX, 5)[0]
    assert oid == 129772


# ------------------------------------------------------------------ энкодер
def _encode_pos_data(
    *, when: datetime, lat: float, lon: float, speed_kmh: float,
    course: int, odometer_km: float, moving: bool, valid: bool, din: int,
) -> bytes:
    ntm = int((when - _EPOCH).total_seconds())
    lat_raw = round(abs(lat) / 90.0 * 0xFFFFFFFF)
    lon_raw = round(abs(lon) / 180.0 * 0xFFFFFFFF)
    flags = (0x01 if valid else 0) | (0x10 if moving else 0) \
        | (0x20 if lat < 0 else 0) | (0x40 if lon < 0 else 0)
    spd = int(round(speed_kmh * 10)) & 0x3FFF
    if course > 255:
        spd |= 0x8000
    body = struct.pack("<III", ntm, lat_raw, lon_raw)
    body += bytes([flags]) + struct.pack("<H", spd) + bytes([course & 0xFF])
    body += int(round(odometer_km * 10)).to_bytes(3, "little")
    body += bytes([din, 0])
    return body


def _encode_appdata(
    *, pid: int, rn: int, oid: int, when: datetime, pos: bytes, extra_sub: bytes = b""
) -> bytes:
    sub = bytes([egts.SR_POS_DATA]) + struct.pack("<H", len(pos)) + pos + extra_sub
    tm = int((when - _EPOCH).total_seconds())
    record = struct.pack("<HHB", len(sub), rn, 0x05)  # RFL: OBFE|TMFE
    record += struct.pack("<II", oid, tm)
    record += bytes([2, 2])  # SST=RST=TELEDATA
    record += sub
    header = bytearray(11)
    header[0] = 0x01
    header[3] = 11
    struct.pack_into("<HH", header, 5, len(record), pid)
    header[9] = egts.PT_APPDATA
    header[10] = egts.crc8(bytes(header[:10]))
    return bytes(header) + record + struct.pack("<H", egts.crc16(record))


def _sample_packet(**overrides) -> bytes:
    params = dict(
        when=datetime(2026, 7, 3, 16, 3, 11, tzinfo=timezone.utc),
        lat=59.7512345, lon=30.4512345, speed_kmh=63.5, course=270,
        odometer_km=48211.7, moving=True, valid=True, din=0x01,
    )
    params.update(overrides)
    pos = _encode_pos_data(**params)
    return _encode_appdata(pid=62639, rn=1, oid=129772,
                           when=params["when"], pos=pos)


# ------------------------------------------------------------------ разбор
def test_roundtrip_pos_data():
    packet = _sample_packet()
    parsed = egts.parse_packet(packet)
    assert parsed.packet_type == egts.PT_APPDATA
    assert parsed.packet_id == 62639
    assert parsed.object_id == 129772

    (rec,) = parsed.records
    assert rec.record_number == 1
    assert rec.record_time == datetime(2026, 7, 3, 16, 3, 11, tzinfo=timezone.utc)
    (pos,) = rec.positions
    assert pos.navigation_time == datetime(2026, 7, 3, 16, 3, 11, tzinfo=timezone.utc)
    assert pos.latitude == pytest.approx(59.7512345, abs=1e-6)
    assert pos.longitude == pytest.approx(30.4512345, abs=1e-6)
    assert pos.speed_kmh == pytest.approx(63.5)
    assert pos.course == 270
    assert pos.odometer_km == pytest.approx(48211.7)
    assert pos.is_moving and pos.is_valid and pos.ignition


def test_ignition_from_movement_or_speed_when_din_empty():
    parsed = egts.parse_packet(_sample_packet(din=0x00, moving=True, speed_kmh=0))
    assert parsed.records[0].positions[0].ignition is True
    # Реальный кейс с машины Т772НХ178: DIN=0, MV=0, но скорость 54 км/ч —
    # двигатель точно работает.
    parsed = egts.parse_packet(_sample_packet(din=0x00, moving=False, speed_kmh=54))
    assert parsed.records[0].positions[0].ignition is True
    parsed = egts.parse_packet(_sample_packet(din=0x00, moving=False, speed_kmh=0))
    assert parsed.records[0].positions[0].ignition is None


def test_ext_pos_data_is_not_treated_as_unknown_or_ignition():
    when = datetime(2026, 7, 6, 12, 34, 11, tzinfo=timezone.utc)
    pos = _encode_pos_data(when=when, lat=59.832085, lon=30.4411716,
                           speed_kmh=0, course=340, odometer_km=88518.5,
                           moving=False, valid=True, din=0)
    ext_pos = bytes([egts.SR_EXT_POS_DATA]) + struct.pack("<H", 2) + bytes.fromhex("08 0a")
    packet = _encode_appdata(pid=12061, rn=1, oid=129772, when=when, pos=pos,
                             extra_sub=ext_pos)

    (rec,) = egts.parse_packet(packet).records
    assert rec.unknown_subrecords == []
    (parsed_pos,) = rec.positions
    assert parsed_pos.ext_pos_data == bytes.fromhex("08 0a")
    # SR_EXT_POS_DATA у Stavtrack — точность/спутники, а не датчик зажигания.
    assert parsed_pos.ignition is None


def test_southern_western_hemispheres():
    parsed = egts.parse_packet(_sample_packet(lat=-33.9, lon=-70.6))
    pos = parsed.records[0].positions[0]
    assert pos.latitude == pytest.approx(-33.9, abs=1e-5)
    assert pos.longitude == pytest.approx(-70.6, abs=1e-5)


def test_corrupted_crc_raises():
    packet = bytearray(_sample_packet())
    packet[-1] ^= 0xFF  # портим CRC16 тела
    with pytest.raises(egts.EgtsParseError):
        egts.parse_packet(bytes(packet))
    packet = bytearray(_sample_packet())
    packet[10] ^= 0xFF  # портим CRC8 заголовка
    with pytest.raises(egts.EgtsParseError):
        egts.parse_packet(bytes(packet))


def test_packet_length_and_tcp_slicing():
    packet = _sample_packet()
    assert egts.packet_length(packet) == len(packet)
    assert egts.packet_length(packet[:4]) is None  # мало данных
    # два пакета склеены в один TCP-read
    stream = packet + packet
    first = egts.packet_length(stream)
    assert first == len(packet)
    assert egts.packet_length(stream[first:]) == len(packet)


def test_state_data_subrecord_parsed_and_unknown_collected():
    when = datetime(2026, 7, 3, 16, 3, 11, tzinfo=timezone.utc)
    pos = _encode_pos_data(when=when, lat=59.75, lon=30.45, speed_kmh=0,
                           course=0, odometer_km=100.0, moving=False, valid=True, din=0)
    # STATE_DATA (тип 20): mode=1, питание 25.5В (потолок байта), АКБ 4.1В,
    # внутр. 3.9В, flags=0b101 + следом неизвестная подзапись типа 99.
    state = bytes([egts.SR_STATE_DATA]) + struct.pack("<H", 5) + bytes([1, 255, 41, 39, 0b101])
    unknown = bytes([99]) + struct.pack("<H", 2) + b"\x01\x02"
    packet = _encode_appdata(pid=1, rn=1, oid=129772, when=when, pos=pos,
                             extra_sub=state + unknown)
    (rec,) = egts.parse_packet(packet).records
    assert rec.state is not None
    assert rec.state.mode == 1
    assert rec.state.main_power_v == pytest.approx(25.5)
    assert rec.state.backup_battery_v == pytest.approx(4.1)
    assert rec.state.flags == 0b101
    assert rec.unknown_subrecords == [99]
    (p,) = rec.positions  # POS_DATA рядом не пострадала
    assert p.latitude == pytest.approx(59.75, abs=1e-5)


def test_build_response_is_valid_egts():
    parsed = egts.parse_packet(_sample_packet())
    ack = egts.build_response(parsed)
    assert ack[0] == 0x01 and ack[9] == egts.PT_RESPONSE
    assert egts.crc8(ack[:10]) == ack[10]
    fdl = struct.unpack_from("<H", ack, 5)[0]
    body = ack[11:11 + fdl]
    assert struct.unpack_from("<H", body, 0)[0] == 62639  # RPID = подтверждаем его PID
    assert body[2] == egts.PC_OK
    assert struct.unpack_from("<H", ack, 11 + fdl)[0] == egts.crc16(body)
