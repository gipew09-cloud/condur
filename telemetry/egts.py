"""
Парсер протокола ЕГТС (ГОСТ Р 54619) для ретрансляции Stavtrack.

Чистый модуль без БД и сети (тестируется как rc_service):
  - parse_packet(bytes)  → ParsedPacket (записи, точки EGTS_SR_POS_DATA);
  - build_response(...)  → байты EGTS_PT_RESPONSE (подтверждение приёма).

Проверено на реальном пакете из логов Railway: CRC8 заголовка совпал (0x77),
OID = 129772 (Stavtrack ID машины), TM — корректное время точки.

Битовые поля и порядок байт — по ГОСТ; всё little-endian.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

# Отсчёт времени ЕГТС: секунды с 2010-01-01 00:00:00 UTC.
_EGTS_EPOCH = datetime(2010, 1, 1, tzinfo=timezone.utc)

# Типы пакетов транспортного уровня.
PT_RESPONSE = 0
PT_APPDATA = 1

# Типы подзаписей, которые разбираем.
SR_RECORD_RESPONSE = 0
SR_POS_DATA = 16
SR_EXT_POS_DATA = 17  # расширение POS_DATA: точность/спутники, НЕ датчики
SR_STATE_DATA = 20  # состояние терминала: режим + напряжения питания

# Коды результата обработки.
PC_OK = 0


class EgtsParseError(ValueError):
    """Пакет не соответствует ЕГТС (битые CRC/длины/версия)."""


def crc8(data: bytes) -> int:
    """CRC8 заголовка (poly 0x31, init 0xFF)."""
    crc = 0xFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def crc16(data: bytes) -> int:
    """CRC16-CCITT тела пакета (poly 0x1021, init 0xFFFF)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass(frozen=True)
class PositionData:
    """Разобранная EGTS_SR_POS_DATA — одна GPS-точка."""
    navigation_time: datetime
    latitude: float          # градусы, юг — отрицательная
    longitude: float         # градусы, запад — отрицательная
    speed_kmh: float
    course: int              # 0–359
    odometer_km: float       # пробег по прибору, км
    is_moving: bool          # флаг MV
    is_valid: bool           # флаг VLD (достоверность координат)
    digital_inputs: int      # байт DIN; зажигание обычно бит 0
    source: int
    ext_pos_data: bytes = b""  # EGTS_SR_EXT_POS_DATA, если пришла рядом

    @property
    def ignition(self) -> bool | None:
        # Бит 0 DIN у части конфигураций — зажигание. Но у Stavtrack датчик
        # зажигания может передаваться вне POS_DATA (DIN=0 и MV=0 даже в
        # движении — видели на реальной машине при 54 км/ч). Поэтому True
        # ставим только по надёжным признакам, а False не выдумываем: если
        # вход пустой и машина стоит, датчик просто не пришёл.
        if bool(self.digital_inputs & 0x01) or self.is_moving or self.speed_kmh > 0:
            return True
        return None


@dataclass(frozen=True)
class TerminalState:
    """EGTS_SR_STATE_DATA: режим терминала и напряжения питания (в вольтах).

    ⚠️ Поля напряжений однобайтовые в 0.1 В — максимум 25.5 В. Для 24-вольтовой
    бортсети грузовика (работа ~28 В) значение может упираться в потолок,
    поэтому для определения зажигания используем с осторожностью.
    """
    mode: int
    main_power_v: float        # напряжение основного питания
    backup_battery_v: float    # резервный АКБ терминала
    internal_battery_v: float  # внутренняя батарейка
    flags: int


@dataclass(frozen=True)
class ServiceRecord:
    record_number: int
    object_id: int | None      # OID — Stavtrack object id (есть при OBFE=1)
    record_time: datetime | None
    service_type: int          # SST
    positions: list[PositionData]
    state: TerminalState | None
    unknown_subrecords: list[int]  # типы подзаписей, которые не разбираем


@dataclass(frozen=True)
class ParsedPacket:
    packet_id: int
    packet_type: int
    records: list[ServiceRecord] = field(default_factory=list)

    @property
    def object_id(self) -> int | None:
        for rec in self.records:
            if rec.object_id is not None:
                return rec.object_id
        return None


def packet_length(buffer: bytes) -> int | None:
    """Полная длина первого пакета в буфере (или None, если данных мало).

    Нужна приёмнику: TCP может склеить несколько пакетов в один read или
    порезать один пакет на части.
    """
    if len(buffer) < 7:
        return None
    hl = buffer[3]
    fdl = struct.unpack_from("<H", buffer, 5)[0]
    return hl + fdl + (2 if fdl else 0)


def parse_packet(data: bytes) -> ParsedPacket:
    """Разбирает один пакет ЕГТС. Кидает EgtsParseError на битых данных."""
    if len(data) < 11:
        raise EgtsParseError(f"слишком короткий пакет: {len(data)} байт")
    if data[0] != 0x01:
        raise EgtsParseError(f"неизвестная версия протокола PRV={data[0]}")

    hl = data[3]
    if hl not in (11, 16) or len(data) < hl:
        raise EgtsParseError(f"некорректная длина заголовка HL={hl}")
    if crc8(data[: hl - 1]) != data[hl - 1]:
        raise EgtsParseError("CRC8 заголовка не сошёлся")

    fdl, pid = struct.unpack_from("<HH", data, 5)
    packet_type = data[9]

    if fdl == 0:
        return ParsedPacket(packet_id=pid, packet_type=packet_type)

    if len(data) < hl + fdl + 2:
        raise EgtsParseError(f"пакет обрезан: ждём {hl + fdl + 2}, есть {len(data)}")
    sfrd = data[hl: hl + fdl]
    sfrcs = struct.unpack_from("<H", data, hl + fdl)[0]
    if crc16(sfrd) != sfrcs:
        raise EgtsParseError("CRC16 тела не сошёлся")

    records: list[ServiceRecord] = []
    if packet_type == PT_APPDATA:
        records = _parse_service_records(sfrd)
    return ParsedPacket(packet_id=pid, packet_type=packet_type, records=records)


def _parse_service_records(sfrd: bytes) -> list[ServiceRecord]:
    records: list[ServiceRecord] = []
    offset = 0
    while offset + 5 <= len(sfrd):
        rl, rn = struct.unpack_from("<HH", sfrd, offset)
        rfl = sfrd[offset + 4]
        offset += 5

        object_id: int | None = None
        record_time: datetime | None = None
        if rfl & 0x01:  # OBFE — есть OID
            object_id = struct.unpack_from("<I", sfrd, offset)[0]
            offset += 4
        if rfl & 0x02:  # EVFE — есть EVID (не используем)
            offset += 4
        if rfl & 0x04:  # TMFE — есть TM
            tm = struct.unpack_from("<I", sfrd, offset)[0]
            record_time = _EGTS_EPOCH + timedelta(seconds=tm)
            offset += 4

        sst = sfrd[offset]
        offset += 2  # SST + RST

        record_data = sfrd[offset: offset + rl]
        offset += rl

        positions, state, unknown = _parse_subrecords(record_data)
        records.append(ServiceRecord(
            record_number=rn, object_id=object_id, record_time=record_time,
            service_type=sst, positions=positions, state=state,
            unknown_subrecords=unknown,
        ))
    return records


def _parse_subrecords(rd: bytes) -> tuple[list[PositionData], TerminalState | None, list[int]]:
    positions: list[PositionData] = []
    state: TerminalState | None = None
    unknown: list[int] = []
    ext_pos_payload: bytes = b""
    offset = 0
    while offset + 3 <= len(rd):
        srt = rd[offset]
        srl = struct.unpack_from("<H", rd, offset + 1)[0]
        offset += 3
        payload = rd[offset: offset + srl]
        offset += srl
        if srt == SR_POS_DATA and len(payload) >= 21:
            positions.append(_parse_pos_data(payload))
        elif srt == SR_EXT_POS_DATA:
            # В логах Stavtrack это выглядит как unknown_sr=[17]. По факту это
            # расширение позиции (точность/спутники), а не состояние датчиков.
            ext_pos_payload = payload
        elif srt == SR_STATE_DATA and len(payload) >= 5:
            state = TerminalState(
                mode=payload[0],
                main_power_v=payload[1] / 10.0,
                backup_battery_v=payload[2] / 10.0,
                internal_battery_v=payload[3] / 10.0,
                flags=payload[4],
            )
        elif srt != SR_RECORD_RESPONSE:
            unknown.append(srt)
    if ext_pos_payload and positions:
        positions = [replace(pos, ext_pos_data=ext_pos_payload) for pos in positions]
    return positions, state, unknown


def _parse_pos_data(p: bytes) -> PositionData:
    ntm, lat_raw, lon_raw = struct.unpack_from("<III", p, 0)
    flags = p[12]
    spd_raw = struct.unpack_from("<H", p, 13)[0]
    dir_low = p[15]
    odm = int.from_bytes(p[16:19], "little")
    din = p[19]
    src = p[20]

    lat = lat_raw * 90.0 / 0xFFFFFFFF
    lon = lon_raw * 180.0 / 0xFFFFFFFF
    if flags & 0x20:  # LAHS: 1 = южная широта
        lat = -lat
    if flags & 0x40:  # LOHS: 1 = западная долгота
        lon = -lon

    speed = (spd_raw & 0x3FFF) / 10.0            # биты 0–13, в 0.1 км/ч
    course = dir_low | (0x100 if spd_raw & 0x8000 else 0)  # DIRH — бит 15

    return PositionData(
        navigation_time=_EGTS_EPOCH + timedelta(seconds=ntm),
        latitude=round(lat, 7),
        longitude=round(lon, 7),
        speed_kmh=speed,
        course=course,
        odometer_km=odm / 10.0,
        is_moving=bool(flags & 0x10),   # MV
        is_valid=bool(flags & 0x01),    # VLD
        digital_inputs=din,
        source=src,
    )


# ---------------------------------------------------------------------------
# Ответ (подтверждение приёма) — без него трекер рвёт связь и шлёт повторно.
# ---------------------------------------------------------------------------
_response_pid = 0


def _next_response_pid() -> int:
    global _response_pid
    _response_pid = (_response_pid + 1) & 0xFFFF
    return _response_pid


def build_response(packet: ParsedPacket, *, result: int = PC_OK) -> bytes:
    """EGTS_PT_RESPONSE: «пакет PID принят, все записи обработаны успешно»."""
    body = bytearray(struct.pack("<HB", packet.packet_id, result))
    for rec in packet.records:
        # Запись-обёртка с подзаписью EGTS_SR_RECORD_RESPONSE (CRN + RST).
        sub = struct.pack("<BH", SR_RECORD_RESPONSE, 3) + struct.pack("<HB", rec.record_number, result)
        body += struct.pack("<HHB", len(sub), rec.record_number, 0)
        body += bytes([rec.service_type, rec.service_type])
        body += sub

    header = bytearray(11)
    header[0] = 0x01          # PRV
    header[3] = 11            # HL
    struct.pack_into("<HH", header, 5, len(body), _next_response_pid())
    header[9] = PT_RESPONSE
    header[10] = crc8(bytes(header[:10]))
    return bytes(header) + bytes(body) + struct.pack("<H", crc16(bytes(body)))
