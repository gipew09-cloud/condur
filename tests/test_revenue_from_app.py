"""«Указать выручку» и «Утвердить» из журнала приложения (28.09.2026).

Владелец: «кнопки „Указать выручку“ нету». Проверяем на живой базе: кнопка
стоит ровно на одной карточке рейса, сумма записывается той же логикой, что в
боте (сумма + событие в журнале), повтор нажатия ничего не ломает, чужой рейс
не трогается.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

pytest.importorskip("aiosqlite")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import BigInteger, select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from app.models import Base, Driver, Event, Owner, Shift, Trip, Vehicle  # noqa: E402
from app.services import revenue_flow  # noqa: E402
from app.web.router import (  # noqa: E402
    api_events, api_trip_revenue_approve, api_trip_revenue_set,
)

NOW = datetime.now(timezone.utc)
REQUEST = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))   # без ботов


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


_ENGINES = []


def _run(scenario):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        while _ENGINES:
            loop.run_until_complete(_ENGINES.pop().dispose())
        loop.close()


async def _db():
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    owner = Owner(telegram_id=1, full_name="Владелец", timezone="Europe/Moscow")
    session.add(owner)
    await session.flush()
    vehicle = Vehicle(owner_id=owner.id, license_plate="А214КМ178", is_active=True)
    driver = Driver(owner_id=owner.id, full_name="Иванов", telegram_id=2)
    session.add_all([vehicle, driver])
    await session.flush()
    shift = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                  started_at=NOW - timedelta(hours=5))
    session.add(shift)
    await session.flush()
    return session, owner, driver, vehicle, shift


async def _trip(session, owner, driver, vehicle, shift, **kw):
    trip = Trip(owner_id=owner.id, shift_id=shift.id, driver_id=driver.id,
                vehicle_id=vehicle.id, origin="Агропарк", destination="Лента Кудрово",
                status="completed", completed_at=NOW - timedelta(hours=1), **kw)
    session.add(trip)
    await session.flush()
    return trip


def test_кнопка_стоит_на_одной_карточке_рейса():
    trip = SimpleNamespace(id=7, status="completed", revenue_rub=None, driver_revenue_pending_rub=None)
    assert revenue_flow.action_for("trip_completed", trip) == {"trip_id": 7, "pending": None}
    assert revenue_flow.action_for("trip_created", trip) is None
    # водитель назвал сумму — решение на его карточке, не на «Груз сдан»
    trip.driver_revenue_pending_rub = Decimal("30000")
    assert revenue_flow.action_for("trip_completed", trip) is None
    assert revenue_flow.action_for("trip_revenue_from_driver", trip) == {"trip_id": 7, "pending": 30000.0}
    # выручка есть или рейс не закончен — кнопки нет
    trip.revenue_rub = Decimal("30000")
    assert revenue_flow.action_for("trip_revenue_from_driver", trip) is None
    trip.revenue_rub, trip.status = None, "in_transit"
    assert revenue_flow.action_for("trip_revenue_from_driver", trip) is None


def test_указать_выручку_из_приложения_и_журнал():
    async def scenario():
        session, owner, driver, vehicle, shift = await _db()
        trip = await _trip(session, owner, driver, vehicle, shift)
        session.add(Event(owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
                          trip_id=trip.id, event_type="trip_completed",
                          created_at=NOW - timedelta(hours=1)))
        await session.commit()

        feed = await api_events(owner, session)
        card = next(e for e in feed["events"] if e["type"] == "trip_completed")
        assert card["revenue_action"] == {"trip_id": trip.id, "pending": None}

        answer = await api_trip_revenue_set(REQUEST, trip.id, owner, session, "45 000")
        assert answer == {"ok": True, "trip_id": trip.id, "revenue": 45000.0}
        assert trip.revenue_rub == Decimal("45000.00")
        logged = (await session.execute(
            select(Event).where(Event.event_type == "trip_revenue_set")
        )).scalars().all()
        assert len(logged) == 1 and logged[0].trip_id == trip.id

        feed = await api_events(owner, session)
        card = next(e for e in feed["events"] if e["type"] == "trip_completed")
        assert card["revenue_action"] is None          # выручка есть — кнопки нет

        bad = await api_trip_revenue_set(REQUEST, trip.id, owner, session, "сорок")
        assert bad.status_code == 400
        await session.close()
    _run(scenario)


def test_утвердить_сумму_водителя_и_повтор_нажатия():
    async def scenario():
        session, owner, driver, vehicle, shift = await _db()
        trip = await _trip(session, owner, driver, vehicle, shift,
                           driver_revenue_pending_rub=Decimal("30000"))
        await session.commit()

        answer = await api_trip_revenue_approve(REQUEST, trip.id, owner, session)
        assert answer == {"ok": True, "trip_id": trip.id, "revenue": 30000.0}
        assert trip.revenue_rub == Decimal("30000.00") and trip.driver_revenue_pending_rub is None
        # связь рвалась, нажали ещё раз — ответ тот же, второй записи нет
        again = await api_trip_revenue_approve(REQUEST, trip.id, owner, session)
        assert again["revenue"] == 30000.0
        approved = (await session.execute(
            select(Event).where(Event.event_type == "trip_revenue_approved")
        )).scalars().all()
        assert len(approved) == 1

        empty = await _trip(session, owner, driver, vehicle, shift)
        await session.commit()
        nothing = await api_trip_revenue_approve(REQUEST, empty.id, owner, session)
        assert nothing.status_code == 409
        await session.close()
    _run(scenario)


def test_чужой_рейс_не_трогается():
    async def scenario():
        session, owner, driver, vehicle, shift = await _db()
        trip = await _trip(session, owner, driver, vehicle, shift)
        stranger = Owner(telegram_id=99, full_name="Другой", timezone="Europe/Moscow")
        session.add(stranger)
        await session.commit()
        with pytest.raises(HTTPException) as err:
            await api_trip_revenue_set(REQUEST, trip.id, stranger, session, "1000")
        assert err.value.status_code == 404
        assert trip.revenue_rub is None
        await session.close()
    _run(scenario)
