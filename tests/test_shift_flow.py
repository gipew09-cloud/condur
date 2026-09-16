"""Начало и конец смены — одна логика для бота и приложения.

Сначала эти тесты закрепили, что делает БОТ (до переноса логики в общий
модуль), потом то же самое проверяется для приложения. Владелец должен
получать одинаковое уведомление, откуда бы водитель ни нажал кнопку.
"""
import asyncio
import os
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
from app.models import Base, Driver, Event, Owner, Shift, Vehicle  # noqa: E402


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
    owner = Owner(telegram_id=111, full_name="Владелец", timezone="Europe/Moscow",
                  notifications_enabled=True)
    session.add(owner)
    await session.flush()
    vehicle = Vehicle(owner_id=owner.id, license_plate="Т557ОС178", is_active=True)
    driver = Driver(owner_id=owner.id, full_name="Саломов Холбек", telegram_id=222,
                    salary_type="per_km", salary_rate=10, is_active=True)
    session.add_all([vehicle, driver])
    await session.commit()
    return session, owner, vehicle, driver


def _bot():
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=1)
    return bot


def _texts(bot):
    return [c.args[1] for c in bot.send_message.await_args_list]


def _state(data=None):
    state = AsyncMock()
    state.get_data.return_value = data or {}
    return state


def test_бот_начинает_смену_пишет_журнал_и_зовёт_владельца():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        message, bot = AsyncMock(), _bot()
        await driver_bot._do_start_shift(
            message, _state(), session, bot, driver, vehicle,
            odometer_start=1913, photo_file_id=None,
        )
        shift = (await session.execute(select(Shift))).scalar_one()
        assert shift.status == "started" and shift.odometer_start == 1913
        event = (await session.execute(
            select(Event).where(Event.event_type == "shift_started")
        )).scalar_one()
        assert event.shift_id == shift.id
        assert event.payload["odometer_start"] == 1913
        assert event.payload["vehicle_id"] == vehicle.id
        texts = _texts(bot)
        assert any("начал смену" in t and "Т557ОС178" in t and "1913" in t for t in texts)
        await session.close()
    _run(scenario)


def test_бот_заканчивает_смену_считает_пробег_и_зовёт_владельца():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        message, bot = AsyncMock(), _bot()
        await driver_bot._do_start_shift(
            message, _state(), session, bot, driver, vehicle,
            odometer_start=1913, photo_file_id=None,
        )
        shift = (await session.execute(select(Shift))).scalar_one()
        bot.send_message.reset_mock()
        await driver_bot._do_end_shift(
            message, _state(), session, bot, driver, shift, odometer_end=1977,
        )
        await session.refresh(shift)
        assert shift.status == "completed" and shift.distance_km == 64
        event = (await session.execute(
            select(Event).where(Event.event_type == "shift_completed")
        )).scalar_one()
        assert event.payload["distance_km"] == 64
        assert event.payload["trips"] == 0
        texts = _texts(bot)
        assert any("завершил смену" in t and "Пробег: 64 км" in t for t in texts)
        await session.close()
    _run(scenario)


def test_бот_без_одометра_не_пишет_нулевой_пробег():
    """Фото-режим: пробег впишет владелец — нули показывать нельзя."""
    async def scenario():
        session, owner, vehicle, driver = await _db()
        message, bot = AsyncMock(), _bot()
        await driver_bot._do_start_shift(
            message, _state(), session, bot, driver, vehicle,
            odometer_start=None, photo_file_id=None,
        )
        shift = (await session.execute(select(Shift))).scalar_one()
        bot.send_message.reset_mock()
        await driver_bot._do_end_shift(
            message, _state(), session, bot, driver, shift, odometer_end=None,
        )
        texts = _texts(bot)
        assert any("завершил смену" in t and "после ввода" in t for t in texts)
        assert not any("Пробег: 0" in t for t in texts)
        await session.close()
    _run(scenario)


# ---------------------------------------------------------- из приложения
from datetime import datetime, timedelta, timezone  # noqa: E402

from app.models import DriverAction  # noqa: E402
from app.services import driver_access_service as access  # noqa: E402
from app.services import driver_actions_service as actions  # noqa: E402


async def _phone(session, driver, device="phone-1"):
    issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=111)
    done = await access.redeem(
        session, token=None, code=issued.code, device_id=device,
        device_label="iPhone", platform="ios", app_version="1.0", ip=None,
    )
    await session.commit()
    return done.driver_session


def _op(op_id, kind, payload=None, **extra):
    body = {"client_op_id": op_id, "type": kind, "payload": payload or {}}
    body.update(extra)
    return body


async def _apply(session, ds, driver, body, now=None):
    out = await actions.apply(session, driver_session=ds, driver=driver, body=body, now=now)
    await session.commit()
    return out


def test_приложение_открывает_и_закрывает_смену_как_бот():
    """Владелец получает тот же текст, что и от бота."""
    async def scenario():
        session, owner, vehicle, driver = await _db()
        ds = await _phone(session, driver)
        start = await _apply(session, ds, driver, _op(
            "op-start-0001", "shift.start", {"vehicle_id": vehicle.id, "odometer": 1913},
        ))
        assert start.body()["ok"] is True
        text = actions.owner_notice(start, driver=driver, tz_name=owner.timezone)
        assert "начал смену" in text and "Т557ОС178" in text and "1913" in text
        event = (await session.execute(
            select(Event).where(Event.event_type == "shift_started")
        )).scalar_one()
        assert event.payload["source"] == "app"

        finish = await _apply(session, ds, driver, _op(
            "op-finish-001", "shift.finish",
            {"shift_id": start.body()["shift_id"], "odometer": 1977},
        ))
        body = finish.body()
        assert body["ok"] is True and body["distance_km"] == 64
        # Зарплату водителю не показываем — решение владельца.
        assert "salary" not in body
        text = actions.owner_notice(finish, driver=driver, tz_name=owner.timezone)
        assert "завершил смену" in text and "Пробег: 64 км" in text
        await session.close()
    _run(scenario)


def test_повтор_того_же_действия_не_создаёт_вторую_смену():
    """Запрос дошёл, ответ потерялся — телефон повторяет тот же номер."""
    async def scenario():
        session, owner, vehicle, driver = await _db()
        ds = await _phone(session, driver)
        body = _op("op-start-0001", "shift.start", {"vehicle_id": vehicle.id})
        first = await _apply(session, ds, driver, body)
        again = await _apply(session, ds, driver, body)
        assert first.body()["shift_id"] == again.body()["shift_id"]
        assert again.duplicate is True
        # Повтор не зовёт владельца второй раз.
        assert actions.owner_notice(again, driver=driver, tz_name=None) is None
        shifts = (await session.execute(select(Shift))).scalars().all()
        assert len(shifts) == 1
        await session.close()
    _run(scenario)


def test_отказ_тоже_окончательный_и_запоминается():
    """Смену уже открыли в боте — приложение получает понятный отказ, и
    повтор того же действия получает тот же отказ, а не новую попытку."""
    async def scenario():
        session, owner, vehicle, driver = await _db()
        ds = await _phone(session, driver)
        await driver_bot._do_start_shift(
            AsyncMock(), _state(), session, _bot(), driver, vehicle,
            odometer_start=1000, photo_file_id=None,
        )
        body = _op("op-start-0002", "shift.start", {"vehicle_id": vehicle.id})
        out = await _apply(session, ds, driver, body)
        assert out.body()["ok"] is False
        assert out.body()["error"] == "shift_already_open"
        again = await _apply(session, ds, driver, body)
        assert again.duplicate and again.body()["error"] == "shift_already_open"
        assert len((await session.execute(select(Shift))).scalars().all()) == 1
        await session.close()
    _run(scenario)


def test_чужую_занятую_машину_взять_нельзя():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        other = Driver(owner_id=owner.id, full_name="Петров", telegram_id=333,
                       salary_type="per_km", salary_rate=10, is_active=True)
        session.add(other)
        await session.commit()
        await driver_bot._do_start_shift(
            AsyncMock(), _state(), session, _bot(), other, vehicle,
            odometer_start=1000, photo_file_id=None,
        )
        ds = await _phone(session, driver)
        out = await _apply(session, ds, driver, _op(
            "op-start-0003", "shift.start", {"vehicle_id": vehicle.id},
        ))
        assert out.body()["error"] == "vehicle_busy"
        assert "Т557ОС178" in out.body()["message"]
        await session.close()
    _run(scenario)


def test_машину_чужого_парка_взять_нельзя():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        stranger = Owner(telegram_id=999, full_name="Чужой", timezone="Europe/Moscow")
        session.add(stranger)
        await session.flush()
        foreign = Vehicle(owner_id=stranger.id, license_plate="А000АА00", is_active=True)
        session.add(foreign)
        await session.commit()
        ds = await _phone(session, driver)
        out = await _apply(session, ds, driver, _op(
            "op-start-0004", "shift.start", {"vehicle_id": foreign.id},
        ))
        assert out.body()["error"] == "vehicle_unknown"
        assert (await session.execute(select(Shift))).scalars().all() == []
        await session.close()
    _run(scenario)


def test_закрыть_нечего_и_одометр_назад_не_крутится():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        ds = await _phone(session, driver)
        nothing = await _apply(session, ds, driver, _op("op-finish-000", "shift.finish"))
        assert nothing.body()["error"] == "no_open_shift"

        await _apply(session, ds, driver, _op(
            "op-start-0005", "shift.start", {"vehicle_id": vehicle.id, "odometer": 2000},
        ))
        back = await _apply(session, ds, driver, _op(
            "op-finish-002", "shift.finish", {"odometer": 1990},
        ))
        assert back.body()["error"] == "odometer_backwards"
        # Отказ не закрыл смену.
        shift = (await session.execute(select(Shift))).scalar_one()
        assert shift.status == "started"
        await session.close()
    _run(scenario)


def test_время_нажатия_с_телефона_если_ему_можно_верить():
    """Смену начали без связи в 06:00, сервер узнал в 07:30 — смена началась
    в 06:00. Но часам «из будущего» или недельной давности не верим."""
    async def scenario():
        session, owner, vehicle, driver = await _db()
        ds = await _phone(session, driver)
        now = datetime.now(timezone.utc)
        pressed = now - timedelta(minutes=90)
        out = await _apply(session, ds, driver, _op(
            "op-start-0006", "shift.start", {"vehicle_id": vehicle.id},
            client_created_at=pressed.isoformat(),
        ), now=now)
        shift = await session.get(Shift, out.body()["shift_id"])
        started = shift.started_at.replace(tzinfo=timezone.utc) if shift.started_at.tzinfo is None else shift.started_at
        assert abs((started - pressed).total_seconds()) < 1
        action = (await session.execute(select(DriverAction))).scalar_one()
        assert action.client_created_at is not None

        assert actions.parse_client_time((now + timedelta(hours=1)).isoformat(), now) is None
        assert actions.parse_client_time((now - timedelta(days=3)).isoformat(), now) is None
        assert actions.parse_client_time("2026-09-16T10:00:00", now) is None  # без пояса
        assert actions.parse_client_time("мусор", now) is None
        await session.close()
    _run(scenario)


def test_кривой_запрос_не_записывается():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        ds_id, driver_id = (await _phone(session, driver)).id, driver.id
        for body in (
            {"type": "shift.start"},
            _op("short", "shift.start"),
            _op("op-bad-00001", "fly.away"),
            _op("op-bad-00002", "shift.start", {"vehicle_id": "abc"}),
            _op("op-bad-00003", "shift.start", {}),
        ):
            # Как в настоящем запросе: всё грузится заново в своей транзакции.
            ds = await session.get(access.DriverSession, ds_id)
            drv = await session.get(Driver, driver_id)
            with pytest.raises(actions.BadRequest):
                await actions.apply(session, driver_session=ds, driver=drv, body=body)
            await session.rollback()
        assert (await session.execute(select(DriverAction))).scalars().all() == []
        await session.close()
    _run(scenario)


def test_одновременный_дубль_откатывается_целиком(monkeypatch):
    """Два одинаковых запроса пришли почти разом: второй не нашёл первого,
    сделал своё и упёрся в уникальный номер. Его смена должна исчезнуть,
    а ответ — совпасть с первым."""
    async def scenario():
        session, owner, vehicle, driver = await _db()
        ds = await _phone(session, driver)
        body = _op("op-race-00001", "shift.start", {"vehicle_id": vehicle.id})
        first = await _apply(session, ds, driver, body)
        first_body = first.body()

        real_find = actions.find_existing
        calls = {"n": 0}

        async def blind_once(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return None          # «не увидел» первый запрос
            return await real_find(*args, **kwargs)

        monkeypatch.setattr(actions, "find_existing", blind_once)
        second = await actions.apply(session, driver_session=ds, driver=driver, body=body)
        replayed = await actions.commit_or_replay(session, driver_id=driver.id, outcome=second)

        assert replayed.duplicate is True
        assert replayed.body()["ok"] is True
        assert replayed.body()["shift_id"] == first_body["shift_id"]
        assert len((await session.execute(select(Shift))).scalars().all()) == 1
        assert len((await session.execute(select(DriverAction))).scalars().all()) == 1
        await session.close()
    _run(scenario)
