"""Чтение протокола FLEX (NTCB) оборудования «Навтелеком».

Зачем он нам. Наш приёмник понимает EGTS и Wialon IPS. Трекер СМАРТ S-2435,
который купил владелец, умеет EGTS, но всё интересное у него — в собственном
протоколе FLEX: моточасы, данные CAN, температура, качество вождения. И
главное: **команды от сервера прибору работают только по FLEX** — без этого
не будет панели управления трекерами в кабинете (PROBLEMS №63).

Источник — официальный документ: «Протокол информационного обмена
оборудования ООО „Навтелеком“», версия 6.1 (wiki.navtelecom.ru →
Разработчикам → Описание протокола). Ссылки на разделы в комментариях.

Что уже умеет модуль:
  • разбирает транспортный заголовок NTCB (раздел 1.1);
  • понимает рукопожатие `*>S:` и согласование `*>FLEX` (раздел 1.2.1)
    и собирает ответы сервера;
  • считает контрольные суммы XOR (заголовок) и CRC8 (сообщения FLEX,
    приложение Б);
  • разбирает массив телеметрических записей `~A` по битовой маске полей
    (приложение А.1), поля 1–24 — номер записи, событие, время, статусы,
    координаты, скорость, курс, пробег, напряжения.

⚠️ Поля после 24-го документ описывает дальше (CAN, ДУТ, тахограф и прочее).
Пока они не разобраны: если прибор их включит, запись читается до 24-го поля,
а остаток складывается в `tail` — данные не теряются и не выдумываются.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

PREAMBLE = b"@NTC"
HEADER_SIZE = 16

# Первый символ сообщения говорит, что это (раздел 1.2):
#   '@' — пакет NTCB с 16-байтовым заголовком
#   '~' — сообщение FLEX
#   0x7F — пинг FLEX, ответа не требует
FLEX_MARK = 0x7E  # '~'
PING = 0x7F


def xor_sum(data: bytes) -> int:
    """Контрольная сумма заголовка NTCB — «исключающее или» по байтам."""
    result = 0
    for byte in data:
        result ^= byte
    return result


def _crc8_table() -> tuple[int, ...]:
    """Таблица CRC8 с полиномом 0x31 — как в приложении Б документа."""
    table = []
    for index in range(256):
        crc = index
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return tuple(table)


CRC8_TABLE = _crc8_table()


def crc8(data: bytes) -> int:
    """CRC8 сообщений FLEX: считается от '~' до последнего байта данных."""
    crc = 0
    for byte in data:
        crc = CRC8_TABLE[crc ^ byte]
    return crc


@dataclass(frozen=True)
class Packet:
    """Пакет NTCB: кто кому и что прислал."""

    receiver_id: int
    sender_id: int
    payload: bytes


def build_packet(receiver_id: int, sender_id: int, payload: bytes) -> bytes:
    """Собрать пакет NTCB для ответа прибору (раздел 1.1).

    Порядок полей: преамбула, кому, от кого, длина, сумма данных, сумма
    заголовка. Все числа — младшим байтом вперёд.
    """
    header = bytearray(PREAMBLE)
    header += receiver_id.to_bytes(4, "little")
    header += sender_id.to_bytes(4, "little")
    header += len(payload).to_bytes(2, "little")
    header.append(xor_sum(payload))
    header.append(xor_sum(bytes(header[:15])))
    return bytes(header) + payload


def parse_packet(buffer: bytes) -> tuple[Packet | None, bytes]:
    """Достать из буфера один пакет NTCB.

    Возвращает (пакет, остаток). Пакета ещё нет целиком — (None, буфер).
    ⚠️ Повреждённый пакет (не та преамбула или не сошлась сумма заголовка)
    по документу остаётся без ответа: возвращаем None и НЕ трогаем буфер —
    решение, рвать ли связь, принимает вызывающий.
    """
    if len(buffer) < HEADER_SIZE:
        return None, buffer
    if not buffer.startswith(PREAMBLE):
        return None, buffer
    if xor_sum(buffer[:15]) != buffer[15]:
        return None, buffer
    length = int.from_bytes(buffer[12:14], "little")
    if len(buffer) < HEADER_SIZE + length:
        return None, buffer
    payload = buffer[HEADER_SIZE:HEADER_SIZE + length]
    packet = Packet(
        receiver_id=int.from_bytes(buffer[4:8], "little"),
        sender_id=int.from_bytes(buffer[8:12], "little"),
        payload=payload,
    )
    return packet, buffer[HEADER_SIZE + length:]


# ─── Рукопожатие и согласование (раздел 1.2.1) ──────────────────────────────

HANDSHAKE_PREFIX = b"*>S:"
HANDSHAKE_ANSWER = b"*<S"
NEGOTIATION_PREFIX = b"*>FLEX"
NEGOTIATION_ANSWER = b"*<FLEX"

PROTOCOL_FLEX = 0xB0


@dataclass(frozen=True)
class Negotiation:
    """Пакет `*>FLEX`: какой версией протокола и какими полями будет говорить прибор."""

    protocol: int
    protocol_version: int
    struct_version: int
    data_size: int          # сколько полей описано битовой маской
    bitfield: bytes         # какие поля прибор передаёт

    def enabled(self) -> list[int]:
        """Номера включённых полей, начиная с 1 (как в приложении А.1).

        ⚠️ Биты идут СТАРШИМ вперёд: поле 1 — это бит 7 нулевого байта
        (таблица «Структура битового поля» в разделе 1.2.1). Сначала я
        написал наоборот, и ошибку поймал настоящий файл прибора: маска
        `ff fe 30 0a 08…` из файла совпала с маской из его конфигурации
        только при чтении со старшего бита.
        """
        numbers = []
        for index in range(self.data_size):
            byte_no, bit_no = divmod(index, 8)
            if byte_no >= len(self.bitfield):
                break
            if self.bitfield[byte_no] & (1 << (7 - bit_no)):
                numbers.append(index + 1)
        return numbers


def parse_handshake(payload: bytes) -> str | None:
    """Идентификатор прибора из `*>S:` — в нём IMEI модема."""
    if not payload.startswith(HANDSHAKE_PREFIX):
        return None
    return payload[len(HANDSHAKE_PREFIX):].decode("latin1").strip("\x00 ")


def parse_negotiation(payload: bytes) -> Negotiation | None:
    """Разобрать `*>FLEX`: версия протокола, версия структуры, маска полей."""
    if not payload.startswith(NEGOTIATION_PREFIX) or len(payload) < 10:
        return None
    protocol, protocol_version, struct_version, data_size = payload[6:10]
    size_bytes = (data_size + 7) // 8
    return Negotiation(
        protocol=protocol,
        protocol_version=protocol_version,
        struct_version=struct_version,
        data_size=data_size,
        bitfield=payload[10:10 + size_bytes],
    )


def negotiation_answer(negotiation: Negotiation) -> bytes:
    """Ответ сервера на согласование — теми же версиями, что прислал прибор."""
    return NEGOTIATION_ANSWER + bytes(
        [negotiation.protocol, negotiation.protocol_version, negotiation.struct_version]
    )


# ─── Телеметрическая запись (приложение А.1) ────────────────────────────────

@dataclass(frozen=True)
class Field:
    number: int
    name: str
    size: int
    fmt: str | None   # формат struct; None — оставляем сырыми байтами


# Поля идут строго по порядку, без разделителей; передаются только те, у
# которых в маске стоит единица (раздел 1.2).
FIELDS: tuple[Field, ...] = (
    Field(1, "record_no", 4, "<I"),
    Field(2, "event_code", 2, "<H"),
    Field(3, "event_at", 4, "<I"),
    Field(4, "device_status", 1, "<B"),
    Field(5, "modules_status1", 1, "<B"),
    Field(6, "modules_status2", 1, "<B"),
    Field(7, "gsm_level", 1, "<B"),
    Field(8, "nav_status", 1, "<B"),
    Field(9, "nav_at", 4, "<I"),
    Field(10, "lat_raw", 4, "<i"),
    Field(11, "lon_raw", 4, "<i"),
    Field(12, "alt_dm", 4, "<i"),
    Field(13, "speed_kmh", 4, "<f"),
    Field(14, "course", 2, "<H"),
    Field(15, "mileage_km", 4, "<f"),
    Field(16, "last_leg_km", 4, "<f"),
    Field(17, "leg_seconds", 2, "<H"),
    Field(18, "leg_valid_seconds", 2, "<H"),
    Field(19, "power_mv", 2, "<H"),
    Field(20, "battery_mv", 2, "<H"),
    Field(21, "ain1_mv", 2, "<H"),
    Field(22, "ain2_mv", 2, "<H"),
    Field(23, "ain3_mv", 2, "<H"),
    Field(24, "ain4_mv", 2, "<H"),
    Field(25, "ain5_mv", 2, "<H"),
    Field(26, "ain6_mv", 2, "<H"),
    Field(27, "ain7_mv", 2, "<H"),
    Field(28, "ain8_mv", 2, "<H"),
    # Биты: вход In1…In8, 1 — датчик сработал.
    Field(29, "inputs1", 1, "<B"),
    Field(30, "inputs2", 1, "<B"),
    # Биты: 1-й…8-й выход, 1 — выход работает.
    Field(31, "outputs1", 1, "<B"),
    Field(32, "outputs2", 1, "<B"),
    Field(33, "pulse_counter1", 4, "<I"),
    Field(34, "pulse_counter2", 4, "<I"),
    Field(35, "fuel_freq1", 2, "<H"),
    Field(36, "fuel_freq2", 2, "<H"),
    # Моточасы по датчику работы генератора, в секундах.
    Field(37, "engine_seconds", 4, "<I"),
    # Поля FLEX 2.0/3.0, которые включает наш прибор. Остальные из промежутка
    # 38–138 пока не описаны: если прибор включит такое, разбор остановится
    # на нём и остаток уйдёт в `tail` — см. parse_record.
    # 8 байт: число видимых спутников ГЛОНАСС, GPS, Galileo, Compass, Beidou,
    # DORIS, IRNSS, QZSS — по одному байту.
    Field(70, "nav_info", 8, None),
    # №71 в таблице — два байта: HDOP и PDOP штатного приёмника.
    Field(71, "dop", 2, None),
    Field(124, "modules_status3", 1, "<B"),
    Field(125, "link_status", 1, "<B"),
    # Биты: SH1 слабый удар, SH2 сильный удар, SH3 перемещение, SH4 наклон,
    # WAKEUP. 1 — датчик сработал.
    Field(139, "accel_sensors", 1, "<B"),
    Field(140, "tilt_vertical", 1, "<B"),
    Field(141, "tilt_plumb", 2, None),
)
FIELDS_BY_NUMBER = {f.number: f for f in FIELDS}

# Координаты передаются в десятитысячных долях минуты: градусы = значение /
# 10000 / 60. Высота — в дециметрах.
COORD_DIVISOR = 600_000


@dataclass
class Record:
    """Одна запись чёрного ящика, разобранная настолько, насколько мы умеем."""

    values: dict = field(default_factory=dict)
    tail: bytes = b""

    @property
    def latitude(self) -> float | None:
        raw = self.values.get("lat_raw")
        return None if raw is None else raw / COORD_DIVISOR

    @property
    def longitude(self) -> float | None:
        raw = self.values.get("lon_raw")
        return None if raw is None else raw / COORD_DIVISOR

    @property
    def altitude_m(self) -> float | None:
        raw = self.values.get("alt_dm")
        return None if raw is None else raw / 10

    @property
    def satellites(self) -> int | None:
        raw = self.values.get("nav_status")
        return None if raw is None else raw >> 2

    @property
    def is_valid(self) -> bool | None:
        """Валидны ли координаты (бит 1 состояния навигационного датчика)."""
        raw = self.values.get("nav_status")
        return None if raw is None else bool(raw & 0b10)

    @property
    def event_at(self) -> datetime | None:
        seconds = self.values.get("event_at")
        if not seconds:
            return None
        return datetime.fromtimestamp(seconds, tz=timezone.utc)

    @property
    def hdop(self) -> int | None:
        """Геометрический фактор: чем меньше, тем точнее координата."""
        raw = self.values.get("dop")
        return None if not raw else raw[0]

    @property
    def shock_sensors(self) -> dict | None:
        """Какие виртуальные датчики акселерометра сработали (поле 139)."""
        raw = self.values.get("accel_sensors")
        if raw is None:
            return None
        return {
            "удар слабый": bool(raw & 0b1),
            "удар сильный": bool(raw & 0b10),
            "перемещение": bool(raw & 0b100),
            "наклон": bool(raw & 0b1000),
            "пробуждение": bool(raw & 0b1_0000),
        }

    @property
    def engine_hours(self) -> float | None:
        """Моточасы прибора (поле 37) — он их считает сам, в секундах."""
        seconds = self.values.get("engine_seconds")
        return None if seconds is None else round(seconds / 3600, 2)

    @property
    def ignition(self) -> bool | None:
        """Двигатель (генератор) запущен — бит 7 статуса модулей 1."""
        raw = self.values.get("modules_status1")
        return None if raw is None else bool(raw & 0b1000_0000)


def parse_record(data: bytes, negotiation: Negotiation) -> tuple[Record, int]:
    """Разобрать одну запись по маске полей. Возвращает (запись, сколько съели).

    ⚠️ Дойдя до поля, которого мы ещё не описали, останавливаемся: размер
    неизвестен, а гадать нельзя — сместимся и прочитаем чужие байты как свои.
    Остаток записи уходит в `tail`.
    """
    record = Record()
    offset = 0
    for number in negotiation.enabled():
        known = FIELDS_BY_NUMBER.get(number)
        if known is None:
            record.tail = data[offset:]
            return record, len(data)
        chunk = data[offset:offset + known.size]
        if len(chunk) < known.size:
            record.tail = data[offset:]
            return record, len(data)
        record.values[known.name] = (
            struct.unpack(known.fmt, chunk)[0] if known.fmt else chunk
        )
        offset += known.size
    return record, offset


# ─── Массив телеметрических записей `~A` (раздел 1.2.1) ────────────────────

ARRAY_MARK = b"~A"
EXTRA_MARK = b"~E"


def parse_array(message: bytes, negotiation: Negotiation) -> list[Record] | None:
    """Разобрать `~A<кол-во><записи><crc8>`.

    None — сообщение не похоже на массив или не сошлась контрольная сумма:
    по документу на такое отвечать нельзя, иначе прибор сочтёт данные
    доставленными и сотрёт их из чёрного ящика.
    """
    if not message.startswith(ARRAY_MARK) or len(message) < 4:
        return None
    if crc8(message[:-1]) != message[-1]:
        return None
    count = message[2]
    body = message[3:-1]
    records: list[Record] = []
    offset = 0
    for _ in range(count):
        record, used = parse_record(body[offset:], negotiation)
        records.append(record)
        offset += used
        if used == 0:
            break
    return records


def array_answer(count: int) -> bytes:
    """Подтверждение массива: `~A<кол-во><crc8>` (раздел 1.2.1).

    ⚠️ Отвечать обязательно. Без подтверждения прибор шлёт тот же пакет
    снова и не отдаёт следующие — трек встанет.
    """
    head = ARRAY_MARK + bytes([count])
    return head + bytes([crc8(head)])


# ─── Файл телеметрии (.tmi), который сохраняет NTC Configurator ────────────

TMI_PROTOCOL_MARK = b"\x80\x00"


@dataclass
class TmiFile:
    """Выгрузка чёрного ящика: что за прибор, за какой период и сами записи."""

    model: str
    started_at: datetime | None
    ended_at: datetime | None
    declared_count: int
    negotiation: Negotiation
    records: list[Record] = field(default_factory=list)


def parse_tmi(data: bytes) -> TmiFile | None:
    """Прочитать файл `.tmi`, сохранённый конфигуратором.

    Строение файла (разобрано по настоящей выгрузке прибора S-2435 20.09.2026,
    в документе протокола его нет — это контейнер самой программы):

        0x00  служебные байты
        0x05  название модели, ASCII
        далее  время начала и конца периода (U32, секунды с 1970)
               и число записей (U16)
        0x80  блок протокола: 0x80 0x00, протокол (0xB0), версия (0x1E = 3.0)
        0x84  битовая маска полей, 32 байта
        0xA4  записи, каждая в слоте одинаковой длины

    ⚠️ В слоте лежит запись, затем пустое место, затем ПОВТОР блока протокола
    с маской. По этому повтору и вычисляется длина слота — так надёжнее, чем
    держать «магическое» число 130.
    """
    if len(data) < 0xA4:
        return None
    # Название модели — «S-2435» и подобные; дальше сразу двоичные поля,
    # поэтому ищем именно образец, а не «печатаемые символы».
    found = re.match(rb"S-\d{4}", data[5:32])
    if not found:
        return None
    model = found.group(0).decode("ascii")
    after = 5 + len(model)
    started, ended, count = struct.unpack_from("<IIH", data, after)

    protocol_block = data[0x80:0x84]
    if protocol_block[:2] != TMI_PROTOCOL_MARK:
        return None
    negotiation = Negotiation(
        protocol=protocol_block[2],
        protocol_version=protocol_block[3],
        struct_version=protocol_block[3],
        data_size=255,
        bitfield=data[0x84:0x84 + 32],
    )

    footer = data[0x80:0xA4]          # блок протокола вместе с маской
    first_record = 0xA4
    repeat = data.find(footer, first_record)
    if repeat < 0:
        return None
    slot = repeat + len(footer) - first_record

    result = TmiFile(
        model=model,
        started_at=datetime.fromtimestamp(started, tz=timezone.utc) if started else None,
        ended_at=datetime.fromtimestamp(ended, tz=timezone.utc) if ended else None,
        declared_count=count,
        negotiation=negotiation,
    )
    for offset in range(first_record, len(data) - slot + 1, slot):
        record, _ = parse_record(data[offset:offset + slot], negotiation)
        result.records.append(record)
    return result
