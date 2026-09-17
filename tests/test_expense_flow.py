"""Расход и SOS — одна логика для бота и приложения.

Как со сменой и рейсом: сначала закреплено, что делает БОТ (тексты владельцу,
кнопки, события), потом то же самое проверяется для приложения.
"""
import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
from app.models import Base, Driver, Event, Expense, Owner, Vehicle  # noqa: E402
from app.services import driver_access_service as access  # noqa: E402
from app.services import driver_actions_service as actions  # noqa: E402
from app.services import driver_photos, shift_service, trip_service  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


_ENGINES = []
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _run(scenario):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        while _ENGINES:
            loop.run_until_complete(_ENGINES.pop().dispose())
        loop.close()


async def _db(*, with_shift=True, with_trip=False):
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
                    phone="+79990001122", salary_type="per_km", salary_rate=10,
                    is_active=True)
    session.add_all([vehicle, driver])
    await session.flush()
    shift = trip = None
    if with_shift:
        shift = await shift_service.start_shift(
            session, owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
            odometer_start=100, photo_file_id=None,
        )
        await session.flush()
        if with_trip:
            trip = await trip_service.create_trip(
                session, shift=shift, origin="Агропарк",
                destination="Лента Кудрово", cargo_name="",
            )
    await session.commit()
    return SimpleNamespace(
        session=session, owner=owner, vehicle=vehicle, driver=driver,
        shift=shift, trip=trip,
    )


def _bot():
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=77)
    return bot


def _message():
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=222)
    message.answer.return_value = SimpleNamespace(chat=SimpleNamespace(id=222), message_id=5)
    return message


def _state(data):
    state = AsyncMock()
    state.get_data.return_value = dict(data)
    return state


async def _events(session, kind):
    return (await session.execute(
        select(Event).where(Event.event_type == kind).order_by(Event.id)
    )).scalars().all()


def _answers(message):
    return [c.args[0] for c in message.answer.await_args_list]


# ------------------------------------------------------------------ бот
def test_бот_расход_без_чека_уходит_владельцу_с_кнопками():
    async def scenario():
        db = await _db(with_trip=True)
        owner_bot = _bot()
        message = _message()
        await driver_bot._finalize_expense(
            state=_state({"category": "fuel", "amount": "4500"}),
            session=db.session, driver=db.driver, receipt_file_id=None,
            source_bot=_bot(), owner_bot=owner_bot, reply_target=message,
        )
        expense = (await db.session.execute(select(Expense))).scalar_one()
        assert expense.status == "pending"
        assert expense.amount_rub == Decimal("4500")
        assert expense.shift_id == db.shift.id and expense.trip_id == db.trip.id
        event = (await _events(db.session, "expense_submitted"))[0]
        assert event.payload == {"expense_id": expense.id, "category": "fuel", "amount": "4500"}
        assert event.trip_id == db.trip.id

        call = owner_bot.send_message.await_args_list[0]
        text = call.args[1]
        assert "💸 Расход от <b>Саломов Холбек</b>." in text
        assert "Категория: Топливо" in text
        liters = trip_service.liters_from_rub(Decimal("4500"))
        assert f"Сумма: <b>4500 (~{liters:.0f} л) ₽</b>" in text
        markup = call.kwargs.get("reply_markup") or call.args[2]
        assert markup.inline_keyboard[0][0].callback_data == f"expense:approve:{expense.id}"
        # Водителю — «отправлен» и сколько литров.
        assert any("Расход отправлен владельцу" in a and "Топливо: 4500 ₽" in a
                   for a in _answers(message))
        await db.session.close()
    _run(scenario)


def test_бот_расход_с_чеком_фото_владельцу():
    async def scenario():
        db = await _db(with_shift=False)
        owner_bot = _bot()
        with patch.object(driver_bot, "transfer_photo_to_owner", AsyncMock()) as transfer:
            await driver_bot._finalize_expense(
                state=_state({"category": "other", "amount": "300",
                              "description": "мойка"}),
                session=db.session, driver=db.driver, receipt_file_id="FILE-1",
                source_bot=_bot(), owner_bot=owner_bot, reply_target=_message(),
            )
        expense = (await db.session.execute(select(Expense))).scalar_one()
        # Без смены — расход всё равно создаётся (Правка 3).
        assert expense.shift_id is None and expense.receipt_photo_url == "FILE-1"
        assert expense.description == "мойка"
        kwargs = transfer.await_args.kwargs
        assert kwargs["source_file_id"] == "FILE-1"
        assert kwargs["caption"] == (
            "💸 Расход от <b>Саломов Холбек</b>.\nКатегория: Прочее\n"
            "Сумма: <b>300 ₽</b>\nОписание: мойка"
        )
        assert kwargs["reply_markup"].inline_keyboard[0][2].callback_data == (
            f"expense:reject:{expense.id}"
        )
        owner_bot.send_message.assert_not_awaited()
        await db.session.close()
    _run(scenario)


def test_бот_повтор_расхода_за_минуту_не_создаёт_второй():
    async def scenario():
        db = await _db()
        for _ in range(2):
            message = _message()
            await driver_bot._finalize_expense(
                state=_state({"category": "parking", "amount": "150"}),
                session=db.session, driver=db.driver, receipt_file_id=None,
                source_bot=_bot(), owner_bot=_bot(), reply_target=message,
            )
        count = len((await db.session.execute(select(Expense))).scalars().all())
        assert count == 1
        assert any("уже был добавлен только что" in a for a in _answers(message))
        await db.session.close()
    _run(scenario)


def test_бот_не_даёт_копить_больше_десяти_расходов():
    async def scenario():
        db = await _db()
        for i in range(10):
            db.session.add(Expense(
                owner_id=db.owner.id, driver_id=db.driver.id, category="fuel",
                amount_rub=Decimal(100 + i), status="pending",
            ))
        await db.session.commit()
        message = _message()
        await driver_bot.btn_expense(message, _state({}), db.session)
        assert any("10 расходов ожидают проверки" in a for a in _answers(message))
        await db.session.close()
    _run(scenario)


def test_бот_sos_зовёт_владельца():
    async def scenario():
        db = await _db(with_trip=True)
        owner_bot = _bot()
        call = AsyncMock()
        call.from_user = SimpleNamespace(id=222)
        call.message = _message()
        await driver_bot.cb_sos_confirm(call, db.session, owner_bot)
        event = (await _events(db.session, "sos"))[0]
        assert event.payload == {"state": "рейс создан, Агропарк → Лента Кудрово"}
        assert event.shift_id == db.shift.id and event.trip_id == db.trip.id
        text = owner_bot.send_message.await_args.args[1]
        assert text == (
            "🆘 <b>SOS</b> от водителя <b>Саломов Холбек</b>!\n"
            "Машина: Т557ОС178\nТелефон: +79990001122\n"
            "Сейчас: рейс создан, Агропарк → Лента Кудрово\n\n"
            "<b>Позвоните водителю немедленно.</b>"
        )
        await db.session.close()
    _run(scenario)


def test_бот_sos_вне_смены():
    async def scenario():
        db = await _db(with_shift=False)
        owner_bot = _bot()
        call = AsyncMock()
        call.from_user = SimpleNamespace(id=222)
        call.message = _message()
        await driver_bot.cb_sos_confirm(call, db.session, owner_bot)
        event = (await _events(db.session, "sos"))[0]
        assert event.payload == {"state": "вне смены"}
        assert "Машина: —" in owner_bot.send_message.await_args.args[1]
        await db.session.close()
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


def test_категории_в_приложении_как_кнопки_бота():
    from app.bots import keyboards as kb
    from app.services import expense_flow

    buttons = [
        b.callback_data.split(":", 1)[1]
        for row in kb.expense_category_keyboard().inline_keyboard for b in row
    ]
    assert [c["code"] for c in expense_flow.categories()] == buttons[:-1]
    assert expense_flow.categories()[0] == {"code": "fuel", "label": "Топливо"}


def test_приложение_шлёт_расход_с_чеком_как_бот():
    async def scenario():
        db = await _db(with_trip=True)
        ds = await _phone(db.session, db.driver)
        saved = await driver_photos.save(
            db.session, driver=db.driver, client_id="receipt-0001",
            kind="receipt", data=_JPEG,
        )
        await db.session.commit()
        out = await _apply(db.session, ds, db.driver, "op-exp-0001", "expense.create",
                           {"category": "fuel", "amount": "4 500", "photo": saved.ref})
        body = out.body()
        assert body["ok"] is True and body["status"] == "accepted"
        expense = (await db.session.execute(select(Expense))).scalar_one()
        assert body["expense_id"] == expense.id
        # Решение владельца и деньги рейса телефону не отдаём.
        assert "fuel_cost" not in body and "decision" not in body
        assert expense.amount_rub == Decimal("4500")
        assert expense.receipt_photo_url == saved.ref
        assert expense.trip_id == db.trip.id and expense.status == "pending"
        event = (await _events(db.session, "expense_submitted"))[0]
        assert event.payload["source"] == "app"
        assert event.payload["amount"] == "4500.00"

        notice = actions.owner_message(out, driver=db.driver, tz_name=None)
        liters = trip_service.liters_from_rub(Decimal("4500"))
        assert notice.text == (
            "💸 Расход от <b>Саломов Холбек</b>.\nКатегория: Топливо\n"
            f"Сумма: <b>4500 (~{liters:.0f} л) ₽</b>"
        )
        assert notice.photo == _JPEG
        assert notice.markup.inline_keyboard[0][0].callback_data == (
            f"expense:approve:{expense.id}"
        )

        # Повтор того же номера — прежний ответ, второго расхода нет.
        again = await _apply(db.session, ds, db.driver, "op-exp-0001", "expense.create",
                             {"category": "fuel", "amount": "4 500", "photo": saved.ref})
        assert again.duplicate is True
        assert actions.owner_message(again, driver=db.driver, tz_name=None) is None
        assert len((await db.session.execute(select(Expense))).scalars().all()) == 1
        await db.session.close()
    _run(scenario)


def test_приложение_расход_без_смены_и_без_чека():
    async def scenario():
        db = await _db(with_shift=False)
        ds = await _phone(db.session, db.driver)
        out = await _apply(db.session, ds, db.driver, "op-exp-0001", "expense.create",
                           {"category": "other", "amount": 300,
                            "description": "  мойка <b>кузова</b> "})
        assert out.body()["ok"] is True
        expense = (await db.session.execute(select(Expense))).scalar_one()
        assert expense.shift_id is None and expense.receipt_photo_url is None
        assert expense.description == "мойка bкузова/b"
        notice = actions.owner_message(out, driver=db.driver, tz_name=None)
        assert notice.photo is None
        assert notice.text.endswith("Сумма: <b>300 ₽</b>\nОписание: мойка bкузова/b")
        await db.session.close()
    _run(scenario)


def test_приложение_расход_отказы_с_причиной():
    async def scenario():
        db = await _db()
        ds = await _phone(db.session, db.driver)
        cases = [
            ({"category": "other", "amount": "300"}, "что за расход"),
            ({"category": "fuel", "amount": "abc"}, "Не похоже на сумму"),
            ({"category": "fuel", "amount": "0"}, "Не похоже на сумму"),
            ({"category": "fuel", "amount": "-5"}, "Не похоже на сумму"),
            ({"category": "fuel", "amount": "NaN"}, "Не похоже на сумму"),
            ({"category": "fuel", "amount": "2000000"}, "Слишком большая"),
            ({"category": "wash", "amount": "300"}, "Неизвестная категория"),
            ({"category": "fuel", "amount": "300", "photo": "app-999"}, "Фото не дошло"),
        ]
        for i, (payload, reason) in enumerate(cases):
            out = await _apply(db.session, ds, db.driver, f"op-bad-{i:04d}",
                               "expense.create", payload)
            body = out.body()
            assert body["status"] == "rejected", payload
            assert reason in body["message"], (payload, body["message"])
        assert (await db.session.execute(select(Expense))).scalars().all() == []

        # Тот же расход дважды за минуту — второй отклонён, как в боте.
        first = await _apply(db.session, ds, db.driver, "op-exp-0001", "expense.create",
                             {"category": "parking", "amount": "150"})
        second = await _apply(db.session, ds, db.driver, "op-exp-0002", "expense.create",
                              {"category": "parking", "amount": "150"})
        assert first.body()["ok"] is True
        assert second.body()["status"] == "rejected"
        assert "уже был добавлен" in second.body()["message"]
        await db.session.close()
    _run(scenario)


def test_приложение_не_копит_больше_десяти_расходов():
    async def scenario():
        db = await _db()
        ds = await _phone(db.session, db.driver)
        for i in range(10):
            db.session.add(Expense(
                owner_id=db.owner.id, driver_id=db.driver.id, category="fuel",
                amount_rub=Decimal(100 + i), status="pending",
            ))
        await db.session.commit()
        out = await _apply(db.session, ds, db.driver, "op-exp-0001", "expense.create",
                           {"category": "toll", "amount": "700"})
        assert out.body()["status"] == "rejected"
        assert "10 расходов ожидают проверки" in out.body()["message"]
        await db.session.close()
    _run(scenario)


def test_чужое_фото_чеком_не_приложить():
    async def scenario():
        db = await _db()
        ds = await _phone(db.session, db.driver)
        other = Driver(owner_id=db.owner.id, full_name="Другой", telegram_id=333,
                       salary_type="per_km", salary_rate=10, is_active=True)
        db.session.add(other)
        await db.session.flush()
        saved = await driver_photos.save(
            db.session, driver=other, client_id="receipt-0002", kind="receipt", data=_JPEG,
        )
        await db.session.commit()
        out = await _apply(db.session, ds, db.driver, "op-exp-0001", "expense.create",
                           {"category": "fuel", "amount": "100", "photo": saved.ref})
        assert out.body()["status"] == "rejected"
        await db.session.close()
    _run(scenario)


def test_приложение_sos_как_бот():
    async def scenario():
        db = await _db(with_trip=True)
        ds = await _phone(db.session, db.driver)
        out = await _apply(db.session, ds, db.driver, "op-sos-0001", "sos.send")
        assert out.body()["ok"] is True
        event = (await _events(db.session, "sos"))[0]
        assert event.payload == {
            "state": "рейс создан, Агропарк → Лента Кудрово", "source": "app",
        }
        notice = actions.owner_message(out, driver=db.driver, tz_name=None)
        assert notice.text.startswith("🆘 <b>SOS</b> от водителя <b>Саломов Холбек</b>!")
        assert "Машина: Т557ОС178" in notice.text
        assert "Телефон: +79990001122" in notice.text
        assert notice.photo is None and notice.markup is None
        await db.session.close()
    _run(scenario)


def test_чек_из_приложения_уходит_на_распознавание(monkeypatch):
    from app.services import receipt_ocr
    from app.web import router as web

    async def scenario():
        db = await _db()
        ds = await _phone(db.session, db.driver)
        saved = await driver_photos.save(
            db.session, driver=db.driver, client_id="receipt-0003", kind="receipt", data=_JPEG,
        )
        await db.session.commit()
        out = await _apply(db.session, ds, db.driver, "op-exp-0001", "expense.create",
                           {"category": "fuel", "amount": "4500", "photo": saved.ref})
        started = []
        monkeypatch.setattr(receipt_ocr, "is_enabled", lambda: True)
        monkeypatch.setattr(web.asyncio, "create_task", lambda coro: started.append(coro))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(owner_bot=_bot())))
        ctx = SimpleNamespace(owner=db.owner, driver=db.driver)
        web._schedule_receipt_check(request, ctx, out)
        assert len(started) == 1
        frame = started[0].cr_frame.f_locals
        assert frame["image_bytes"] == _JPEG and frame["typed"] == Decimal("4500.00")
        started[0].close()

        # Распознавание выключено — ничего не запускаем.
        monkeypatch.setattr(receipt_ocr, "is_enabled", lambda: False)
        web._schedule_receipt_check(request, ctx, out)
        assert len(started) == 1
        await db.session.close()
    _run(scenario)
