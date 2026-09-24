"""Фильтр «скачок GPS» не должен запирать машину на старом месте.

Логи 23.09.2026, Т772НХ178: последняя настоящая точка — Шушары. Дальше 17
минут трекер слал (0, 0): ехал без спутников. Спутники вернулись в Обухово,
в 9 км. Фильтр сравнил новую точку со временем ПОСЛЕДНЕГО ПАКЕТА (минуту
назад), а не с временем последней настоящей точки, — «9 км за 50 с» — и
браковал все следующие точки час подряд. У Ставтрэка машина стояла на своём
месте, у нас — «в каком-то непонятном месте».
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test-secret-please-set-a-long-one-in-prod")

import pytest  # noqa: E402

pytest.importorskip("aiosqlite")

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from app.models import Base, Owner, Vehicle, VehicleState, VehicleTelemetryPoint  # noqa: E402
from app.services import telemetry_service as T  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint(type_, compiler, **kw):
    return "INTEGER"


SHUSHARY = (Decimal("59.782923"), Decimal("30.496273"))    # последняя настоящая точка
OBUKHOVO = (59.856845, 30.425747)                          # где машина на самом деле
FIX_AT = datetime(2026, 9, 23, 17, 16, 0, tzinfo=timezone.utc)
LAST_ZERO_PACKET_AT = datetime(2026, 9, 23, 17, 32, 46, tzinfo=timezone.utc)
BACK_ON_SATELLITES = datetime(2026, 9, 23, 17, 33, 36, tzinfo=timezone.utc)


def _scenario(body):
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                owner = Owner(telegram_id=1, full_name="Владелец")
                s.add(owner)
                await s.flush()
                truck = Vehicle(owner_id=owner.id, license_plate="Т772НХ178")
                s.add(truck)
                await s.flush()
                fix = VehicleTelemetryPoint(
                    owner_id=owner.id, vehicle_id=truck.id, observed_at=FIX_AT,
                    latitude=SHUSHARY[0], longitude=SHUSHARY[1], speed_kmh=Decimal(27),
                    is_valid=True,
                )
                s.add(fix)
                await s.flush()
                # Так состояние выглядит после 17 минут пакетов без спутников:
                # место старое, а «последний раз на связи» — минуту назад.
                state = VehicleState(
                    vehicle_id=truck.id, last_point_id=fix.id,
                    last_seen_at=LAST_ZERO_PACKET_AT,
                    latitude=SHUSHARY[0], longitude=SHUSHARY[1], is_valid=True,
                )
                s.add(state)
                await s.commit()
                await body(s, state)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_anchor_time_is_the_last_real_fix_not_the_last_packet():
    async def body(s, state):
        at, lat, lon = await T.jump_anchor(s, state)
        assert at.replace(tzinfo=timezone.utc) == FIX_AT
        assert (lat, lon) == SHUSHARY
    _scenario(body)


def test_car_back_on_satellites_after_a_gap_is_not_a_jump():
    async def body(s, state):
        prev_at, lat, lon = await T.jump_anchor(s, state)
        prev_at = prev_at.replace(tzinfo=timezone.utc)
        # 9 км за 17 минут — обычная езда, а не «скачок».
        assert T.gps_jump_reason(prev_at, lat, lon, BACK_ON_SATELLITES, *OBUKHOVO) is None
        # Как было: время последнего пакета — и та же точка браковалась.
        old = T.gps_jump_reason(LAST_ZERO_PACKET_AT, lat, lon, BACK_ON_SATELLITES, *OBUKHOVO)
        assert old and "скачок GPS" in old
    _scenario(body)


def test_real_spike_right_after_a_fix_is_still_caught():
    """Защита от подмены у Пулково остаётся: 9 км через 40 секунд после
    настоящей точки — скачок."""
    async def body(s, state):
        prev_at, lat, lon = await T.jump_anchor(s, state)
        prev_at = prev_at.replace(tzinfo=timezone.utc)
        reason = T.gps_jump_reason(prev_at, lat, lon, prev_at + timedelta(seconds=40), *OBUKHOVO)
        assert reason and "скачок GPS" in reason
    _scenario(body)


def test_no_anchor_when_state_has_no_real_point():
    async def body(s, state):
        state.last_point_id = None
        assert await T.jump_anchor(s, state) == (None, None, None)
        state.is_valid = False
        assert await T.jump_anchor(s, state) == (None, None, None)
        assert await T.jump_anchor(s, None) == (None, None, None)
    _scenario(body)
