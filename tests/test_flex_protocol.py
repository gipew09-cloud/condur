"""Чтение протокола FLEX (Навтелеком) — по официальному описанию версии 6.1.

Проверяем то, на чём такой разбор обычно и ломается: контрольные суммы,
порядок байт, маска полей и подтверждения. Без подтверждения прибор шлёт
пакет заново и не отдаёт следующие — трек встанет, а мы этого не увидим.
"""
import struct

from app.telemetry import flex


def _negotiation(fields: list[int], *, data_size: int = 24) -> flex.Negotiation:
    """Согласование, в котором включены указанные номера полей."""
    bits = bytearray((data_size + 7) // 8)
    for number in fields:
        byte_no, bit_no = divmod(number - 1, 8)
        bits[byte_no] |= 1 << (7 - bit_no)
    return flex.Negotiation(
        protocol=flex.PROTOCOL_FLEX,
        protocol_version=30,
        struct_version=30,
        data_size=data_size,
        bitfield=bytes(bits),
    )


def test_контрольная_сумма_заголовка_как_в_документе():
    # Раздел 1.1: XOR по байтам.
    assert flex.xor_sum(b"") == 0
    assert flex.xor_sum(b"\x01\x02") == 3
    # Пустой пакет от сервера к устройству: n=0, обе суммы считаются.
    packet = flex.build_packet(receiver_id=0, sender_id=1, payload=b"")
    assert packet.startswith(b"@NTC")
    assert len(packet) == 16
    assert packet[15] == flex.xor_sum(packet[:15])


def test_crc8_совпадает_с_таблицей_из_приложения_Б():
    # Первые значения таблицы из документа: 0x00, 0x31, 0x62, 0x53…
    assert flex.CRC8_TABLE[:4] == (0x00, 0x31, 0x62, 0x53)
    assert flex.CRC8_TABLE[255] == 0xAC


def test_пакет_собирается_и_разбирается_обратно():
    payload = b"*<S"
    raw = flex.build_packet(receiver_id=7, sender_id=1, payload=payload)
    packet, rest = flex.parse_packet(raw)
    assert rest == b""
    assert packet.receiver_id == 7
    assert packet.sender_id == 1
    assert packet.payload == payload


def test_неполный_и_битый_пакет_не_разбираются():
    raw = flex.build_packet(1, 0, b"*<S")
    # Пришла только половина — ждём остальное, буфер не трогаем.
    assert flex.parse_packet(raw[:10]) == (None, raw[:10])
    # Испорчен заголовок — по документу ответа быть не должно.
    broken = bytearray(raw)
    broken[15] ^= 0xFF
    assert flex.parse_packet(bytes(broken))[0] is None


def test_рукопожатие_отдаёт_идентификатор_прибора():
    ident = flex.parse_handshake(b"*>S:863151076883336")
    assert ident == "863151076883336"
    assert flex.parse_handshake(b"~A\x01") is None


def test_согласование_версий_и_маски_полей():
    negotiation = _negotiation([1, 2, 3, 10, 11])
    raw = (
        flex.NEGOTIATION_PREFIX
        + bytes([flex.PROTOCOL_FLEX, 30, 30, negotiation.data_size])
        + negotiation.bitfield
    )
    parsed = flex.parse_negotiation(raw)
    assert parsed.protocol == flex.PROTOCOL_FLEX
    assert parsed.protocol_version == 30   # FLEX 3.0
    assert parsed.struct_version == 30
    assert parsed.enabled() == [1, 2, 3, 10, 11]
    # Ответ сервера повторяет версии прибора.
    assert flex.negotiation_answer(parsed) == flex.NEGOTIATION_ANSWER + bytes([0xB0, 30, 30])


def test_запись_читается_по_маске_включённых_полей():
    """Считаем только то, что прибор включил, и в том порядке, что в таблице."""
    negotiation = _negotiation([1, 2, 3, 8, 10, 11, 13])
    body = (
        struct.pack("<I", 129)          # номер записи
        + struct.pack("<H", 0xA217)     # код события
        + struct.pack("<I", 1789751000)  # время события
        + bytes([0b0100_0111])          # навигация: включён, валидна, 17 спутников
        + struct.pack("<i", 35_950_000)  # широта: 59.9166…°
        + struct.pack("<i", 18_180_000)  # долгота: 30.3°
        + struct.pack("<f", 64.5)       # скорость
    )
    record, used = flex.parse_record(body, negotiation)
    assert used == len(body)
    assert record.values["record_no"] == 129
    assert record.values["event_code"] == 0xA217
    assert record.is_valid is True
    assert record.satellites == 17
    assert round(record.latitude, 4) == 59.9167
    assert round(record.longitude, 4) == 30.3
    assert record.values["speed_kmh"] == 64.5
    assert record.event_at.year == 2026


def test_неизвестное_поле_не_ломает_разбор_а_уходит_в_хвост():
    """⚠️ Полей после 24-го мы ещё не описали: гадать размер нельзя."""
    negotiation = _negotiation([1, 40], data_size=48)
    body = struct.pack("<I", 5) + b"\x01\x02\x03"
    record, used = flex.parse_record(body, negotiation)
    assert record.values["record_no"] == 5
    assert record.tail == b"\x01\x02\x03"
    assert used == len(body)


def test_массив_записей_и_подтверждение():
    negotiation = _negotiation([1, 2])
    one = struct.pack("<I", 1) + struct.pack("<H", 0xFF00)
    two = struct.pack("<I", 2) + struct.pack("<H", 0x0001)
    head = flex.ARRAY_MARK + bytes([2]) + one + two
    message = head + bytes([flex.crc8(head)])

    records = flex.parse_array(message, negotiation)
    assert [r.values["record_no"] for r in records] == [1, 2]
    # 0xFF00 — пакет текущего состояния (раздел 1.2.1), у него нулевой индекс.
    assert records[0].values["event_code"] == 0xFF00

    answer = flex.array_answer(2)
    assert answer[:2] == flex.ARRAY_MARK
    assert answer[2] == 2
    assert answer[3] == flex.crc8(answer[:3])


def test_массив_с_битой_суммой_не_подтверждается():
    """Если ответить на испорченный массив, прибор сотрёт данные из памяти."""
    negotiation = _negotiation([1])
    head = flex.ARRAY_MARK + bytes([1]) + struct.pack("<I", 7)
    broken = head + bytes([(flex.crc8(head) + 1) % 256])
    assert flex.parse_array(broken, negotiation) is None

# ─── Проверка на настоящих байтах прибора ──────────────────────────────────

# Маска полей из файла ТМИ, выгруженного с прибора S-2435 (IMEI …336) 20.09.2026.
# Та же маска лежит и в его конфигурации (`PROTOCOL/FLEX mask=…`) — совпадение
# этих двух источников и поймало ошибку в порядке битов.
REAL_BITFIELD = bytes.fromhex(
    "fffe300a0800000002000000000000080020000000000000000000000000000000"[:64]
)
# Запись № 153 из того же файла, первые 51 байт — поля, которые мы умеем читать.
REAL_RECORD = bytes.fromhex(
    "990000000a1741fcaf6a000b206315023b3d4b"
    "000000000000000000000000000000000000000000000000"
    "3f0e000012000000"
)


def test_настоящая_запись_прибора_читается():
    """Файл ТМИ с живого прибора — лучшая проверка: он не прощает догадок."""
    negotiation = flex.Negotiation(
        protocol=flex.PROTOCOL_FLEX, protocol_version=30, struct_version=30,
        data_size=255, bitfield=REAL_BITFIELD,
    )
    # Прибор включил ровно эти поля.
    assert negotiation.enabled()[:17] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12,
                                          13, 14, 15, 19, 20]
    assert 29 in negotiation.enabled() and 37 in negotiation.enabled()

    record, _ = flex.parse_record(REAL_RECORD, negotiation)
    assert record.values["record_no"] == 153
    assert record.event_at.strftime("%d.%m.%Y") == "20.09.2026"
    # Прибор лежал на столе: внешнего питания нет, живёт на своей батарее.
    assert record.values["power_mv"] == 0
    assert 3000 < record.values["battery_mv"] < 4200
    # 99 — «нет сигнала сотовой сети» (симки не было).
    assert record.values["gsm_level"] == 99
    # Спутники уже виделись, но решения ещё нет.
    assert record.satellites == 5
    assert record.is_valid is False

def test_файл_тми_читается_целиком():
    """Файл `.tmi` из конфигуратора: заголовок, маска и слоты записей.

    Строение файла в документе протокола не описано — разобрано по настоящей
    выгрузке прибора. Поэтому собираем такой же файл сами и проверяем, что
    читалка находит модель, период, маску и все записи.
    """
    import struct as _struct

    protocol_block = b"\x80\x00" + bytes([flex.PROTOCOL_FLEX, 30])
    mask = bytes.fromhex("fffe300a" + "00" * 28)          # поля 1–15, 19, 20
    footer = protocol_block + mask
    header = (
        b"\x01\x00\x80\x00\x03"
        + b"S-2435"
        + _struct.pack("<II", 1789750000, 1789753600)
        + _struct.pack("<H", 2)
    )
    header = header.ljust(0x80, b"\x00") + footer            # блок протокола в 0x80

    def slot(number: int) -> bytes:
        body = (
            _struct.pack("<I", number)      # 1 номер записи
            + _struct.pack("<H", 0x170A)    # 2 код события
            + _struct.pack("<I", 1789750100)  # 3 время
            + bytes([0, 0x0B, 0x20, 99, 0b0001_0101])  # 4–8 статусы, GSM 99, 5 спутников
            + _struct.pack("<I", 1789750100)  # 9 время валидных
            + _struct.pack("<iii", 0, 0, 0)   # 10–12 координаты и высота
            + _struct.pack("<f", 0.0)         # 13 скорость
            + _struct.pack("<H", 0)           # 14 курс
            + _struct.pack("<f", 0.0)         # 15 пробег
            + _struct.pack("<HH", 0, 3647)    # 19 питание, 20 батарея
        )
        return body.ljust(94, b"\x00") + footer

    data = header + slot(65) + slot(66)
    parsed = flex.parse_tmi(data)
    assert parsed.model == "S-2435"
    assert parsed.declared_count == 2
    assert len(parsed.records) == 2
    assert [r.values["record_no"] for r in parsed.records] == [65, 66]
    first = parsed.records[0]
    assert first.satellites == 5 and first.is_valid is False
    assert first.values["battery_mv"] == 3647
    assert first.values["power_mv"] == 0      # внешнего питания нет
    # Длина слота вычисляется по повтору блока протокола, а не «магическим» числом.
    assert first.tail == b""


def test_датчики_акселерометра_разбираются_по_битам():
    negotiation = _negotiation([1, 139], data_size=144)
    body = struct.pack("<I", 1) + bytes([0b0001_1100])
    record, _ = flex.parse_record(body, negotiation)
    sensors = record.shock_sensors
    assert sensors["перемещение"] is True
    assert sensors["наклон"] is True
    assert sensors["пробуждение"] is True
    assert sensors["удар слабый"] is False

