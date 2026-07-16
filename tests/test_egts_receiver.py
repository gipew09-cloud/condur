from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services import telemetry_service
from app.telemetry.egts_receiver import ReceiverConfig, _env_int, _peer_parts, _preview_hex


def test_env_int_uses_default_and_bounds(monkeypatch):
    monkeypatch.delenv("EGTS_PORT", raising=False)
    assert _env_int("EGTS_PORT", 9000, minimum=1, maximum=65535) == 9000

    monkeypatch.setenv("EGTS_PORT", "bad")
    assert _env_int("EGTS_PORT", 9000, minimum=1, maximum=65535) == 9000

    monkeypatch.setenv("EGTS_PORT", "70000")
    assert _env_int("EGTS_PORT", 9000, minimum=1, maximum=65535) == 65535


def test_receiver_config_prefers_egts_port(monkeypatch):
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("EGTS_PORT", "9000")
    monkeypatch.setenv("EGTS_MAX_PACKET_BYTES", "512")
    monkeypatch.setenv("EGTS_IDLE_TIMEOUT_SECONDS", "30")

    cfg = ReceiverConfig.from_env()

    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000
    assert cfg.max_packet_bytes == 512
    assert cfg.idle_timeout_seconds == 30


def test_receiver_config_falls_back_to_port(monkeypatch):
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.delenv("EGTS_PORT", raising=False)

    assert ReceiverConfig.from_env().port == 8080


def test_peer_parts_and_preview_hex():
    assert _peer_parts(("127.0.0.1", 12345)) == ("127.0.0.1", 12345)
    assert _peer_parts(None) == (None, None)
    assert _preview_hex(bytes.fromhex("010203040506"), limit=4) == "01 02 03 04"


def test_vehicle_motion_status():
    assert telemetry_service.vehicle_motion_status(Decimal("54"), False) == "moving"
    assert telemetry_service.vehicle_motion_status(Decimal("0"), True) == "idle_engine"
    assert telemetry_service.vehicle_motion_status(Decimal("0"), False) == "stopped"


def test_motion_status_text_and_duration_label():
    start = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2, minutes=15)

    assert telemetry_service.motion_status_text("moving", Decimal("54")) == "едет · 54 км/ч"
    assert telemetry_service.motion_status_text("idle_engine") == "стоит, двигатель работает"
    assert telemetry_service.duration_label(start, end) == "2 ч 15 мин"


def test_vehicle_control_signal_priorities():
    assert telemetry_service.vehicle_control_signal(
        motion_status="moving", has_active_shift=False, has_active_trip=False
    ) == "moving_without_shift"
    assert telemetry_service.vehicle_control_signal(
        motion_status="moving", has_active_shift=True, has_active_trip=False
    ) == "moving_without_trip"
    assert telemetry_service.vehicle_control_signal(
        motion_status="idle_engine", has_active_shift=True, has_active_trip=True
    ) == "idle_engine"
    assert telemetry_service.vehicle_control_signal(
        motion_status="moving", has_active_shift=True, has_active_trip=True, gps_stale=True
    ) == "gps_stale"
    assert telemetry_service.vehicle_control_signal(
        motion_status="moving", has_active_shift=True, has_active_trip=True, gps_invalid=True
    ) == "gps_invalid"


# =========================================================================
# Инцидент 16.07.2026: рассинхронизированное соединение висело 1ч40м и
# молча глотало пакеты двух машин — они «пропали» с карты, хотя Stavtrack
# продолжал ретранслировать. Причина: packet_length() верила мусорным
# байтам и ждала «пакет» выдуманной длины, а каждый новый байт сбрасывал
# idle-таймаут. Теперь такой поток закрывается сразу.
# =========================================================================
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.telemetry import egts


def _make_header(fdl: int = 0, hl: int = 11, prv: int = 0x01, good_crc: bool = True) -> bytes:
    """Транспортный заголовок ЕГТС: PRV SKID FLAGS HL HE FDL(2) PID(2) PT CRC8."""
    head = bytes([prv, 0x00, 0x00, hl, 0x00]) + fdl.to_bytes(2, "little") + b"\x01\x00" + b"\x00"
    pad = b"\x00" * (hl - 1 - len(head))
    head = head + pad
    crc = egts.crc8(head) if good_crc else (egts.crc8(head) ^ 0xFF)
    return head + bytes([crc])


def test_header_error_detects_garbage():
    assert egts.header_error(b"") is None                      # мало данных — ждём
    assert egts.header_error(_make_header()) is None           # валидный заголовок
    assert "PRV" in egts.header_error(b"\x77aaaaaaaaaa")       # не ЕГТС
    bad_hl = bytes([0x01, 0, 0, 99, 0, 0, 0])
    assert "HL" in egts.header_error(bad_hl)                   # бредовая длина заголовка
    assert "CRC8" in egts.header_error(_make_header(good_crc=False))


def _run_handler(data: bytes, idle_timeout: int = 30):
    """Прогоняет handle_client на подсунутых байтах. Возвращает (обработано, время)."""
    from app.telemetry.egts_receiver import ReceiverConfig, handle_client

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        # eof НЕ шлём: соединение «живое», как у ретранслятора
        writer = MagicMock()
        writer.get_extra_info.return_value = ("10.0.0.1", 5555)
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
        cfg = ReceiverConfig(host="0.0.0.0", port=1, max_packet_bytes=512, idle_timeout_seconds=idle_timeout)
        with patch("app.telemetry.egts_receiver._process_packet", new=AsyncMock(return_value=b"")) as proc:
            # 2 секунды: настоящий фикс закрывает соединение мгновенно;
            # старый код ждал бы idle_timeout или «пакет» выдуманной длины
            await asyncio.wait_for(handle_client(reader, writer, cfg), timeout=2)
            return proc.await_count

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_garbage_stream_closes_immediately_not_hangs():
    # мусор (не ЕГТС) на живом соединении → закрыли сразу, ничего не «обработали»
    assert _run_handler(b"\x77" * 40) == 0


def test_bogus_huge_length_closes_immediately():
    # валидный CRC заголовка, но заявленная длина больше лимита → закрываем,
    # а не копим байты часами (сценарий инцидента)
    assert _run_handler(_make_header(fdl=0xFFF0)) == 0


def test_valid_packet_then_garbage_processes_first():
    # нормальный пакет обработан, мусор после него рвёт соединение
    assert _run_handler(_make_header(fdl=0) + b"\x77" * 20) == 1


# ------------------------------------------- аналитика приездов на РЦ
def test_typical_time_of_day_label():
    assert telemetry_service.typical_time_of_day_label([]) is None
    # приезды к ~8 утра, один ночной выброс не сдвигает медиану
    minutes = [7 * 60 + 50, 8 * 60 + 10, 8 * 60 + 30, 2 * 60]
    assert telemetry_service.typical_time_of_day_label(minutes) == "08:10"
    assert telemetry_service.typical_time_of_day_label([505]) == "08:25"


def test_best_arrival_hour_needs_enough_visits():
    # в 7 утра выгрузка быстрее, чем в 10 — но нужен минимум 2 приезда в час
    waits = [(7, 40), (7, 50), (10, 90), (10, 120), (13, 5)]
    hour, avg = telemetry_service.best_arrival_hour(waits)
    assert hour == 7 and avg == 45  # 13:00 с одним приездом не считается
    assert telemetry_service.best_arrival_hour([(9, 30)]) is None
    assert telemetry_service.best_arrival_hour([]) is None
