"""Один расход топлива в кабинете и в приложении.

Владелец 16.09.2026: «как так получилось, что на сайте 21 л машина потратила
за сутки, а в приложении всего 10 л» — и «нужно просто сделать одинаковый
расход». Расходились окно (скользящие сутки против «с полуночи») и точки
(сводка брала уровень бака только из пакетов с достоверным GPS, хотя на
стоянке трекер шлёт нули вместо координат, а бак меряет честно).
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

pytest.importorskip("aiogram")
pytest.importorskip("aiosqlite")

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from app.models import (  # noqa: E402
    Base, Owner, Vehicle, VehicleTelemetryPoint, VehicleTelemetryRawPacket,
)
from app.services.timeutil import owner_tz  # noqa: E402
from app.web.router import api_vehicle_fuel, api_vehicle_summary  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


# Прямая тарировка: сырое значение = литры. Так проще читать тест.
CALIBRATION = [[0, 0], [1000, 1000]]


def test_кабинет_и_сводка_показывают_один_расход():
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            session = async_sessionmaker(engine, expire_on_commit=False)()
            owner = Owner(telegram_id=1, full_name="Владелец", timezone="Europe/Moscow")
            session.add(owner)
            await session.flush()
            vehicle = Vehicle(
                owner_id=owner.id, license_plate="Т557ОС178", is_active=True,
                fuel_calibration=CALIBRATION, tank_litres=600,
            )
            session.add(vehicle)
            await session.flush()
            raw = VehicleTelemetryRawPacket(
                vehicle_id=vehicle.id, terminal_id="1",
                payload=b"\x00", payload_size=1,
                received_at=datetime.now(timezone.utc),
            )
            session.add(raw)
            await session.flush()

            # Сегодня по часам владельца: с полуночи уровень упал 400 → 380.
            tz = owner_tz(owner.timezone)
            local = datetime.now(tz)
            midnight = datetime(local.year, local.month, local.day, tzinfo=tz)
            start = midnight.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)
            span = max((now - start).total_seconds(), 120)

            def point(at, litres, valid=True):
                return VehicleTelemetryPoint(
                    raw_packet_id=raw.id, owner_id=owner.id,
                    vehicle_id=vehicle.id, terminal_id="1",
                    observed_at=at,
                    latitude=Decimal("59.8") if valid else Decimal("0"),
                    longitude=Decimal("30.4") if valid else Decimal("0"),
                    speed_kmh=Decimal("0"), fuel_level_raw=litres,
                    is_valid=valid,
                )

            session.add_all([
                # Вчера вечером — должно попасть только в «скользящие сутки».
                point(start - timedelta(hours=3), 450),
                point(start + timedelta(seconds=span * .1), 400),
                point(start + timedelta(seconds=span * .5), 390),
                # Стоянка: координат нет, а уровень бака настоящий.
                point(start + timedelta(seconds=span * .9), 380, valid=False),
            ])
            await session.commit()

            cabinet = await api_vehicle_fuel(
                vehicle.id, owner, session, hours=24, period="today",
            )
            app_summary = await api_vehicle_summary(vehicle.id, owner, session)

            assert cabinet["period"] == "today"
            assert cabinet["summary"]["spent_l"] == pytest.approx(20, abs=0.5)
            assert app_summary["fuel_spent_label"] == "20 л"
            await session.close()
        finally:
            await engine.dispose()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
