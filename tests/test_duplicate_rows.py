"""
Регресс-тесты на падения из-за дубликатов строк в БД (лог Railway 2026-07-10):
`MultipleResultsFound: Multiple rows were found when one or none was required`.

Дубликаты открытых смен/рейсов могли появиться от двойного нажатия кнопок.
Код не должен из-за них падать — берём самую свежую запись.
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

from app.models import Base, Driver, Owner, Shift, Trip, Vehicle  # noqa: E402
from app.services import shift_service, trip_service  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


@pytest.fixture()
def loop_run():
    loop = asyncio.new_event_loop()
    yield loop.run_until_complete
    loop.close()


async def _make_db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed(session):
    owner = Owner(telegram_id=1, full_name="Владелец")
    session.add(owner)
    await session.flush()
    driver = Driver(
        owner_id=owner.id, telegram_id=2, full_name="Водитель",
        salary_type="per_km", salary_rate=Decimal(0),
    )
    vehicle = Vehicle(owner_id=owner.id, license_plate="А001АА")
    session.add_all([driver, vehicle])
    await session.flush()
    return owner, driver, vehicle


def test_two_open_shifts_returns_newest_not_crash(loop_run):
    """Раньше: MultipleResultsFound на КАЖДОМ действии водителя."""
    async def scenario():
        engine, sessionmaker = await _make_db()
        async with sessionmaker() as session:
            owner, driver, vehicle = await _seed(session)
            now = datetime.now(timezone.utc)
            for started in (now - timedelta(hours=2), now):
                session.add(Shift(
                    owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                    status="started", started_at=started,
                ))
            await session.commit()

            shift = await shift_service.get_active_shift(session, driver.id)
            assert shift is not None
            assert shift.id == 2  # взяли самую свежую
        await engine.dispose()

    loop_run(scenario())


def test_two_open_trips_returns_newest_not_crash(loop_run):
    async def scenario():
        engine, sessionmaker = await _make_db()
        async with sessionmaker() as session:
            owner, driver, vehicle = await _seed(session)
            shift = Shift(
                owner_id=owner.id, driver_id=driver.id,
                vehicle_id=vehicle.id, status="started",
            )
            session.add(shift)
            await session.flush()
            for status in ("created", "in_transit"):
                session.add(Trip(
                    owner_id=owner.id, shift_id=shift.id, driver_id=driver.id,
                    vehicle_id=vehicle.id, status=status,
                ))
            await session.commit()

            trip = await trip_service.get_active_trip(session, shift.id)
            assert trip is not None and trip.id == 2  # самый свежий
        await engine.dispose()

    loop_run(scenario())


def test_no_open_shift_or_trip_returns_none(loop_run):
    """Обратная сторона фикса: пусто — по-прежнему None, а не исключение."""
    async def scenario():
        engine, sessionmaker = await _make_db()
        async with sessionmaker() as session:
            _, driver, _ = await _seed(session)
            await session.commit()
            assert await shift_service.get_active_shift(session, driver.id) is None
            assert await trip_service.get_active_trip(session, 999) is None
        await engine.dispose()

    loop_run(scenario())


def test_no_show_detector_survives_duplicate_shifts(loop_run):
    """Точный сценарий из лога Railway: у водителя 2 открытые смены."""
    from unittest.mock import AsyncMock, MagicMock, patch

    async def scenario():
        engine, sessionmaker = await _make_db()
        async with sessionmaker() as session:
            owner, driver, vehicle = await _seed(session)
            now = datetime.now(timezone.utc)
            for _ in range(2):
                session.add(Shift(
                    owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                    status="started", started_at=now,
                ))
            await session.commit()

        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch("app.services.scheduler_jobs.async_session", sessionmaker), \
             patch("app.services.scheduler_jobs.logger") as fake_logger:
            from app.services.scheduler_jobs import no_show_detector_job

            await no_show_detector_job(bot)
            # ни одного «no_show_detector_job failed» в логах
            fake_logger.exception.assert_not_called()
        await engine.dispose()

    loop_run(scenario())


def test_weekly_review_fires_after_daily_summary():
    """Баг: недельная сводка приходила ДО дневной. Теперь: дневная в 21:00,
    недельная в воскресенье 21:30 — строго после."""
    import inspect

    from app.services import scheduler_jobs as sj

    daily_src = inspect.getsource(sj.daily_summary_job)
    weekly_src = inspect.getsource(sj.weekly_review_job)
    # дневная: окно 21:00..21:29
    assert "local.hour != 21 or local.minute >= 30" in daily_src
    # недельная: воскресенье, окно 21:30..21:59 (после дневной)
    assert "local.weekday() != 6 or local.hour != 21 or local.minute < 30" in weekly_src
