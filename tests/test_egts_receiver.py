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
