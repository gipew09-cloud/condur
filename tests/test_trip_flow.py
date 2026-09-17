"""Рейс — одна логика для бота и приложения.

Как и со сменой (`test_shift_flow.py`): сначала закреплено, что делает БОТ,
потом то же самое проверяется для приложения. Владелец получает одинаковые
уведомления, откуда бы водитель ни нажал кнопку.
"""
import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

pytest.importorskip("aiogram")
pytest.importorskip("aiosqlite")

from sqlalchemy import BigInteger, select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from app.bots import driver_bot  # noqa: E402
from app.models import (  # noqa: E402
    Base, Driver, Event, Expense, Owner, RouteTemplate, Shift, Trip, Vehicle,
)
from app.services import driver_access_service as access  # noqa: E402
from app.services import driver_actions_service as actions  # noqa: E402
from app.services import shift_service  # noqa: E402


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


async def _db(*, with_shift=True):
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    owner = Owner(telegram_id=111, full_name="Владелец", timezone="Europe/Moscow",
                  notifications_enabled=True)
    session.add(owner)
    await session.flush()
    vehicle = Vehicle(owner_id=owner.id, license_plate="Т557ОС178", is_active=True)
    driver = Driver(owner_id=owner.id, full_name="Саломов Холбек", telegram_id=222,
                    salary_type="per_km", salary_rate=10, is_active=True)
    session.add_all([vehicle, driver])
    await session.flush()
    routes = [
        RouteTemplate(owner_id=owner.id, name="Шушары", origin="Агропарк",
                      destination="Пятёрочка Шушары", default_cargo="Овощи",
                      sort_order=20, is_active=True),
        RouteTemplate(owner_id=owner.id, name="Лен", origin="Агропарк",
                      destination="Лента Кудрово", sort_order=10, is_active=True),
        # То же место, другое написание — должно попасть в ту же папку.
        RouteTemplate(owner_id=owner.id, name="Шушары-2", origin="Агропарк ",
                      destination="Магнит Шушары", sort_order=30, is_active=True),
        RouteTemplate(owner_id=owner.id, name="Софийская", origin="Софийская 60",
                      destination="Перекрёсток", sort_order=0, is_active=True),
        RouteTemplate(owner_id=owner.id, name="Старый", origin="Агропарк",
                      destination="Закрытый РЦ", sort_order=5, is_active=False),
    ]
    session.add_all(routes)
    if with_shift:
        await shift_service.start_shift(
            session, owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
            odometer_start=100, photo_file_id=None,
        )
    await session.commit()
    return session, owner, vehicle, driver, routes


def _bot():
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=77)
    return bot


def _texts(bot):
    return [c.args[1] for c in bot.send_message.await_args_list]


def _message():
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=222)
    message.answer.return_value = SimpleNamespace(chat=SimpleNamespace(id=222), message_id=5)
    return message


def _state():
    state = AsyncMock()
    state.get_data.return_value = {}
    return state


async def _events(session, kind):
    return (await session.execute(
        select(Event).where(Event.event_type == kind).order_by(Event.id)
    )).scalars().all()


# ------------------------------------------------------------------ бот
def test_бот_создаёт_рейс_и_зовёт_владельца():
    async def scenario():
        session, owner, vehicle, driver, _ = await _db()
        bot = _bot()
        await driver_bot._finalize_new_trip(
            _message(), _state(), session, bot, driver,
            origin="Агропарк", destination="Пятёрочка Шушары", cargo="Овощи",
        )
        trip = (await session.execute(select(Trip))).scalar_one()
        assert trip.status == "created" and trip.vehicle_id == vehicle.id
        event = (await _events(session, "trip_created"))[0]
        assert event.trip_id == trip.id
        assert event.payload["origin"] == "Агропарк"
        assert any("создал рейс" in t and "Пятёрочка Шушары" in t and "Овощи" in t
                   for t in _texts(bot))
        await session.close()
    _run(scenario)


def test_бот_выехал_и_сдал_груз_топливо_и_кнопка_выручки():
    async def scenario():
        session, owner, vehicle, driver, _ = await _db()
        bot = _bot()
        message = _message()
        await driver_bot._finalize_new_trip(
            message, _state(), session, bot, driver,
            origin="Агропарк", destination="Лента Кудрово", cargo=None,
        )
        trip = (await session.execute(select(Trip))).scalar_one()
        session.add(Expense(
            owner_id=owner.id, driver_id=driver.id, shift_id=trip.shift_id,
            trip_id=trip.id, category="fuel", amount_rub=Decimal("3400"),
            status="pending",
        ))
        await session.commit()
        bot.send_message.reset_mock()

        await driver_bot._do_depart(message, _state(), session, bot, location=None)
        await session.refresh(trip)
        assert trip.status == "in_transit"
        assert len(await _events(session, "trip_in_transit")) == 1
        assert any("выехал" in t and "Лента Кудрово" in t for t in _texts(bot))

        bot.send_message.reset_mock()
        await driver_bot._do_end_trip(message, _state(), session, bot, location=None)
        await session.refresh(trip)
        assert trip.status == "completed" and trip.completed_at is not None
        assert trip.fuel_cost_rub == Decimal("3400.00")
        done = (await _events(session, "trip_completed"))[0]
        assert done.payload["fuel_cost"] == "3400.00"
        call = bot.send_message.await_args_list[0]
        assert "завершил рейс" in call.args[1] and "3400" in call.args[1]
        # Владельцу — кнопка «Указать выручку».
        markup = call.kwargs.get("reply_markup") or call.args[2]
        assert "trip:revenue:" in markup.inline_keyboard[0][0].callback_data
        prompt = (await _events(session, "trip_revenue_prompt"))[0]
        assert prompt.payload["owner_msg_id"] == 77
        assert prompt.payload["driver_msg_id"] == 5
        await session.close()
    _run(scenario)


def test_бот_без_выезда_не_завершает_созданный_рейс():
    async def scenario():
        session, owner, vehicle, driver, _ = await _db()
        bot = _bot()
        await driver_bot._finalize_new_trip(
            _message(), _state(), session, bot, driver,
            origin="Агропарк", destination="Лента Кудрово", cargo=None,
        )
        await driver_bot._do_end_trip(_message(), _state(), session, bot, location=None)
        trip = (await session.execute(select(Trip))).scalar_one()
        assert trip.status == "created"
        await session.close()
    _run(scenario)


# ------------------------------------------------------------ приложение
async def _phone(session, driver, device="phone-1"):
    issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=111)
    done = await access.redeem(
        session, token=None, code=issued.code, device_id=device,
        device_label="iPhone", platform="ios", app_version="1.0", ip=None,
    )
    await session.commit()
    return done.driver_session


async def _apply(session, ds, driver, op_id, kind, payload=None):
    out = await actions.apply(
        session, driver_session=ds, driver=driver,
        body={"client_op_id": op_id, "type": kind, "payload": payload or {}},
    )
    await session.commit()
    return out


def test_маршруты_для_телефона_по_складам_как_в_боте():
    from app.services import trip_flow

    async def scenario():
        session, owner, *_ = await _db()
        catalog = await trip_flow.route_catalog(session, owner.id)
        assert [g["origin"] for g in catalog] == ["Агропарк", "Софийская 60"]
        # Порядок — как расставил владелец; закрытый маршрут не виден;
        # «Агропарк » с пробелом — в той же папке.
        assert [r["destination"] for r in catalog[0]["routes"]] == [
            "Лента Кудрово", "Пятёрочка Шушары", "Магнит Шушары",
        ]
        assert catalog[0]["routes"][1]["cargo"] == "Овощи"
        # И ровно так же, как видит водитель в боте.
        assert [g["origin"] for g in catalog] == await driver_bot._route_origins(session, owner.id)
        for folder in catalog:
            in_bot = await driver_bot._templates_for_origin(session, owner.id, folder["origin"])
            assert [r["id"] for r in folder["routes"]] == [t.id for t in in_bot]
        await session.close()
    _run(scenario)


def test_приложение_ведёт_рейс_как_бот():
    async def scenario():
        session, owner, vehicle, driver, routes = await _db()
        ds = await _phone(session, driver)
        created = await _apply(session, ds, driver, "op-trip-0001", "trip.create",
                               {"template_id": routes[0].id})
        body = created.body()
        assert body["ok"] is True and body["status"] == "accepted"
        trip = (await session.execute(select(Trip))).scalar_one()
        assert body["trip_id"] == trip.id
        assert trip.origin == "Агропарк" and trip.destination == "Пятёрочка Шушары"
        assert trip.cargo_name == "Овощи"
        text = actions.owner_message(created, driver=driver, tz_name=None).text
        assert "создал рейс" in text and "Пятёрочка Шушары" in text
        event = (await _events(session, "trip_created"))[0]
        assert event.payload["source"] == "app"

        departed = await _apply(session, ds, driver, "op-trip-0002", "trip.depart",
                                {"trip_id": trip.id})
        assert departed.body()["ok"] is True
        text = actions.owner_message(departed, driver=driver, tz_name=None).text
        assert "выехал" in text

        finished = await _apply(session, ds, driver, "op-trip-0003", "trip.finish", {})
        assert finished.body()["ok"] is True
        await session.refresh(trip)
        assert trip.status == "completed"
        notice = actions.owner_message(finished, driver=driver, tz_name=None)
        text, markup = notice.text, notice.markup
        assert "завершил рейс" in text
        assert "trip:revenue:" in markup.inline_keyboard[0][0].callback_data
        # Деньги водителю в ответ не уходят.
        assert "fuel_cost" not in finished.body()
        await session.close()
    _run(scenario)


def test_рейс_без_смены_второй_рейс_и_чужой_маршрут_отклоняются():
    async def scenario():
        session, owner, vehicle, driver, routes = await _db(with_shift=False)
        ds = await _phone(session, driver)
        no_shift = await _apply(session, ds, driver, "op-trip-0001", "trip.create",
                                {"template_id": routes[0].id})
        assert no_shift.body()["status"] == "rejected"
        assert "начните смену" in no_shift.body()["message"]

        await shift_service.start_shift(
            session, owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
            odometer_start=None, photo_file_id=None,
        )
        await session.commit()
        closed = await _apply(session, ds, driver, "op-trip-0002", "trip.create",
                              {"template_id": routes[4].id})
        assert closed.body()["status"] == "rejected"

        other = Owner(telegram_id=999, full_name="Чужой", timezone="Europe/Moscow")
        session.add(other)
        await session.flush()
        foreign = RouteTemplate(owner_id=other.id, name="x", origin="x",
                                destination="y", is_active=True)
        session.add(foreign)
        await session.commit()
        stolen = await _apply(session, ds, driver, "op-trip-0003", "trip.create",
                              {"template_id": foreign.id})
        assert stolen.body()["status"] == "rejected"

        first = await _apply(session, ds, driver, "op-trip-0004", "trip.create",
                             {"template_id": routes[0].id})
        assert first.body()["status"] == "accepted"
        second = await _apply(session, ds, driver, "op-trip-0005", "trip.create",
                              {"template_id": routes[1].id})
        assert second.body()["status"] == "rejected"
        assert "Уже открыт рейс" in second.body()["message"]
        assert len((await session.execute(select(Trip))).scalars().all()) == 1
        await session.close()
    _run(scenario)


def test_порядок_рейса_соблюдается():
    """Нельзя «Сдал груз» до «Выехал», нельзя выехать дважды."""
    async def scenario():
        session, owner, vehicle, driver, routes = await _db()
        ds = await _phone(session, driver)
        await _apply(session, ds, driver, "op-trip-0001", "trip.create",
                     {"template_id": routes[0].id})
        early = await _apply(session, ds, driver, "op-trip-0002", "trip.finish", {})
        assert early.body()["status"] == "rejected"
        await _apply(session, ds, driver, "op-trip-0003", "trip.depart", {})
        twice = await _apply(session, ds, driver, "op-trip-0004", "trip.depart", {})
        assert twice.body()["status"] == "rejected"
        stale = await _apply(session, ds, driver, "op-trip-0005", "trip.finish",
                             {"trip_id": 99999})
        assert stale.body()["status"] == "rejected"
        await session.close()
    _run(scenario)


def test_смену_с_открытым_рейсом_не_закрыть():
    """Как в боте: «Сначала завершите открытый рейс»."""
    async def scenario():
        session, owner, vehicle, driver, routes = await _db()
        ds = await _phone(session, driver)
        await _apply(session, ds, driver, "op-trip-0001", "trip.create",
                     {"template_id": routes[0].id})
        out = await _apply(session, ds, driver, "op-shift-0001", "shift.finish", {})
        assert out.body()["status"] == "rejected"
        assert "завершите открытый рейс" in out.body()["message"]
        shift = (await session.execute(select(Shift))).scalar_one()
        assert shift.status == "started"
        await session.close()
    _run(scenario)


def test_отклонённое_топливо_не_входит_в_расход_рейса():
    """Прочие расходы отклонённые уже не считали, а топливо — считало."""
    async def scenario():
        session, owner, vehicle, driver, routes = await _db()
        bot = _bot()
        message = _message()
        await driver_bot._finalize_new_trip(
            message, _state(), session, bot, driver,
            origin="Агропарк", destination="Лента Кудрово", cargo=None,
        )
        trip = (await session.execute(select(Trip))).scalar_one()
        for amount, status in (("3400", "approved"), ("900", "pending"), ("5000", "rejected")):
            session.add(Expense(
                owner_id=owner.id, driver_id=driver.id, shift_id=trip.shift_id,
                trip_id=trip.id, category="fuel", amount_rub=Decimal(amount), status=status,
            ))
        await session.commit()
        await driver_bot._do_depart(message, _state(), session, bot, location=None)
        await driver_bot._do_end_trip(message, _state(), session, bot, location=None)
        await session.refresh(trip)
        assert trip.fuel_cost_rub == Decimal("4300.00")
        await session.close()
    _run(scenario)
