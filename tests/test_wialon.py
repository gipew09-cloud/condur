"""
Тесты приёмника Wialon IPS — протокола, в котором Stavtrack передаёт датчики
(зажигание и т.п.). Логин-строка взята из реального лога 16.07.2026:
приёмник записал `23 4c 23 31 32 38 35 30 37 3b 4e 41 0d 0a` = "#L#128507;NA".
"""
import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

from app.telemetry import wialon


# ------------------------------------------------------------- разбор строк
def test_login_v11_from_real_log():
    raw = bytes.fromhex("234c233132383530373b4e41").decode()  # "#L#128507;NA"
    msg = wialon.parse_message(raw)
    assert msg.kind == "L" and msg.terminal_id == "128507"
    assert wialon.ack_for(msg) == b"#AL#1\r\n"


def test_login_v20():
    msg = wialon.parse_message("#L#2.0;129772;NA;AB12")
    assert msg.kind == "L" and msg.terminal_id == "129772"


def test_ping():
    msg = wialon.parse_message("#P#")
    assert msg.kind == "P"
    assert wialon.ack_for(msg) == b"#AP#\r\n"


def test_short_data_coordinates():
    # 5951.6834;N = 59°51.6834' → 59.86139;  03028.8636;E = 30°28.8636' → 30.48106
    msg = wialon.parse_message("#SD#160726;205000;5951.6834;N;03028.8636;E;54;120;15;8")
    assert msg.kind == "SD" and len(msg.points) == 1
    p = msg.points[0]
    assert p.observed_at == datetime(2026, 7, 16, 20, 50, 0, tzinfo=timezone.utc)
    assert abs(p.latitude - 59.86139) < 0.0001
    assert abs(p.longitude - 30.48106) < 0.0001
    assert p.speed_kmh == 54 and p.is_valid
    assert wialon.ack_for(msg) == b"#ASD#1\r\n"


def test_full_data_ignition_from_params():
    line = ("#D#160726;205000;5951.6834;N;03028.8636;E;54;120;15;8;"
            "1.2;0;0;NA;NA;ign:1:1,pwr_ext:2:27.9")
    msg = wialon.parse_message(line)
    p = msg.points[0]
    assert p.ignition is True
    assert p.params["pwr_ext"] == 27.9
    assert wialon.ack_for(msg) == b"#AD#1\r\n"


def test_ignition_fallback_to_inputs_bit():
    # именованного параметра нет → бит 0 поля inputs
    on = wialon.parse_message(
        "#D#160726;205000;5951.6834;N;03028.8636;E;0;0;15;8;1.2;1;0;NA;NA;"
    ).points[0]
    off = wialon.parse_message(
        "#D#160726;205000;5951.6834;N;03028.8636;E;0;0;15;8;1.2;0;0;NA;NA;"
    ).points[0]
    assert on.ignition is True and off.ignition is False
    # в коротком SD датчиков нет вообще → зажигание неизвестно
    sd = wialon.parse_message("#SD#160726;205000;5951.6834;N;03028.8636;E;0;0;15;8")
    assert sd.points[0].ignition is None


def test_batch_and_na_values():
    msg = wialon.parse_message(
        "#B#160726;205000;5951.6834;N;03028.8636;E;10;0;15;8|"
        "NA;NA;NA;N;NA;E;NA;NA;NA;NA"
    )
    assert msg.kind == "B" and len(msg.points) == 2
    assert msg.points[0].is_valid and not msg.points[1].is_valid
    assert wialon.ack_for(msg) == b"#AB#2\r\n"


def test_garbage_is_unknown():
    assert wialon.parse_message("hello").kind == "?"
    assert wialon.parse_message("#XYZ").kind == "?"


# ------------------------------------------------- сессия на общем порту
def _run_receiver(data: bytes, store_mock: AsyncMock):
    from app.telemetry.egts_receiver import ReceiverConfig, handle_client

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        writer = MagicMock()
        writer.get_extra_info.return_value = ("10.0.0.1", 5555)
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
        cfg = ReceiverConfig(host="0.0.0.0", port=1, max_packet_bytes=4096, idle_timeout_seconds=10)
        with patch("app.telemetry.egts_receiver._store_wialon_points", new=store_mock):
            await asyncio.wait_for(handle_client(reader, writer, cfg), timeout=2)
        return writer

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_receiver_switches_to_wialon_and_acks():
    """Один порт — два протокола: '#' в начале потока включает wialon-режим."""
    store = AsyncMock()
    writer = _run_receiver(
        b"#L#128507;NA\r\n"
        b"#SD#160726;205000;5951.6834;N;03028.8636;E;54;120;15;8\r\n"
        b"#P#\r\n",
        store,
    )
    sent = b"".join(c.args[0] for c in writer.write.call_args_list)
    assert b"#AL#1\r\n" in sent      # логин подтверждён
    assert b"#ASD#1\r\n" in sent     # точка подтверждена
    assert b"#AP#\r\n" in sent       # пинг подтверждён
    store.assert_awaited_once()
    args, kwargs = store.await_args
    assert args[0] == "128507" and len(args[1]) == 1  # терминал и одна точка


def test_receiver_wialon_data_without_login_drops():
    store = AsyncMock()
    _run_receiver(b"#SD#160726;205000;5951.6834;N;03028.8636;E;54;120;15;8\r\n", store)
    store.assert_not_awaited()
