"""Произвольный период трека и список смен для вкладки «Треки».

Владелец 27.08.2026: «треки недоделаны», нужен плеер и произвольный период
«с… по…». Раньше трек умел только «последние N часов» — рабочий день за
прошлый вторник посмотреть было нельзя.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

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

from app.models import Base, Driver, Owner, Shift, Trip, Vehicle  # noqa: E402
from app.web.router import _period_bounds, api_vehicle_shifts  # noqa: E402

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


def test_period_from_to_wins_over_hours():
    since, until = _period_bounds("2026-08-20T07:00", "2026-08-20T20:00", 12, NOW)
    assert since == datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc)
    assert until == datetime(2026, 8, 20, 20, 0, tzinfo=timezone.utc)


def test_period_survives_swapped_ends():
    """Владелец выбрал «с 20:00 по 07:00» — не пустой ответ, а тот же день."""
    since, until = _period_bounds("2026-08-20T20:00", "2026-08-20T07:00", 12, NOW)
    assert since.hour == 7 and until.hour == 20


def test_period_falls_back_to_hours():
    since, until = _period_bounds(None, None, 6, NOW)
    assert until == NOW
    assert NOW - since == timedelta(hours=6)


def test_period_is_capped_at_a_month():
    """Трек за год — миллионы точек, браузер владельца ляжет.

    Отдаём последний месяц периода, а не пустой ответ: пустой экран читается
    как «данных нет», и владелец будет искать несуществующую поломку.
    """
    since, until = _period_bounds("2025-08-20T00:00", "2026-08-20T00:00", 12, NOW)
    assert until - since == timedelta(days=31)
    assert until == datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)


def test_broken_dates_do_not_break_the_track():
    since, until = _period_bounds("вчера", "сегодня", 3, NOW)
    assert NOW - since == timedelta(hours=3)


def _run(scenario):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        loop.close()


async def _db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    session = maker()
    owner = Owner(telegram_id=1, full_name="Владелец", timezone="Europe/Moscow")
    session.add(owner)
    await session.flush()
    vehicle = Vehicle(owner_id=owner.id, license_plate="Т557ОС178", is_active=True)
    driver = Driver(owner_id=owner.id, full_name="Саломов", telegram_id=2)
    session.add_all([vehicle, driver])
    await session.flush()
    return session, owner, vehicle, driver


def test_shifts_carry_route_and_odometer_mileage():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        shift = Shift(
            owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
            started_at=datetime(2026, 8, 20, 4, 12, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 20, 16, 47, tzinfo=timezone.utc),
            odometer_start=100000, odometer_end=100214,
        )
        session.add(shift)
        await session.flush()
        session.add_all([
            Trip(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                 shift_id=shift.id, origin="склад 5.18", destination="РЦ 7 шагов"),
            Trip(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                 shift_id=shift.id, origin="РЦ 7 шагов", destination="база"),
        ])
        await session.commit()

        data = await api_vehicle_shifts(
            vehicle.id, owner, session,
            frm="2026-08-20T00:00", to="2026-08-21T00:00",
        )
        row = data["shifts"][0]
        assert row["route"] == "склад 5.18 → РЦ 7 шагов → база"
        assert row["trips"] == 2
        assert row["driver"] == "Саломов"
        assert row["is_open"] is False
        await session.close()
    _run(scenario)


def test_open_shift_shows_up_and_mileage_is_not_faked():
    """Открытая смена попадает в список, а пробег без одометра — не ноль."""
    async def scenario():
        session, owner, vehicle, driver = await _db()
        session.add(Shift(
            owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
            started_at=datetime(2026, 8, 30, 5, 0, tzinfo=timezone.utc),
            ended_at=None, odometer_start=None, odometer_end=None,
        ))
        await session.commit()

        data = await api_vehicle_shifts(
            vehicle.id, owner, session,
            frm="2026-08-30T00:00", to="2026-08-30T23:59",
        )
        row = data["shifts"][0]
        assert row["is_open"] is True
        assert row["ended_label"] is None
        # ⚠️ ноль читался бы как «никуда не ездил»
        assert row["distance_km"] is None
        assert row["route"] is None
        await session.close()
    _run(scenario)


def test_shifts_of_another_owner_are_not_returned():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        stranger = Owner(telegram_id=99, full_name="Чужой")
        session.add(stranger)
        await session.flush()
        session.add(Shift(
            owner_id=stranger.id, driver_id=driver.id, vehicle_id=vehicle.id,
            started_at=datetime(2026, 8, 30, 5, 0, tzinfo=timezone.utc),
        ))
        await session.commit()

        data = await api_vehicle_shifts(
            vehicle.id, owner, session,
            frm="2026-08-30T00:00", to="2026-08-30T23:59",
        )
        assert data["shifts"] == []
        await session.close()
    _run(scenario)
