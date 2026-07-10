"""
Тесты потока выручки рейса «одна живая кнопка» (баг: выручка задваивалась,
когда владелец параллельно открывал ввод текстом и жал «Одобрить»).

Гоняем НАСТОЯЩИЕ хендлеры owner_bot/driver_bot на in-memory SQLite
(JSONB/BigInteger подменяются при компиляции DDL) с мокнутыми ботами Telegram.

Запуск: pytest tests/test_revenue_flow.py
"""
import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

pytest.importorskip("aiogram")
pytest.importorskip("aiosqlite")

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from sqlalchemy import BigInteger, select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from app.bots import keyboards as kb  # noqa: E402
from app.bots.driver_bot import (  # noqa: E402
    cb_driver_revenue,
    driver_revenue_amount,
    driver_router,
)
from app.bots.owner_bot import (  # noqa: E402
    cb_trip_revenue_approve,
    cb_trip_revenue_edit,
    cb_trip_revenue_start,
    set_trip_revenue,
)
from app.bots.states import DriverTripRevenue, NewTrip, SetTripRevenue  # noqa: E402
from app.models import Base, Driver, Event, Owner, Shift, Trip, Vehicle  # noqa: E402
from app.services import trip_service  # noqa: E402
from app.services.event_service import log_event  # noqa: E402


# --- SQLite-совместимость DDL (в бою — Postgres, тут — только для тестов) ---
@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    # В SQLite автоинкремент работает только с типом INTEGER.
    return "INTEGER"


OWNER_TG = 111
DRIVER_TG = 222
OWNER_CHAT = 111
DRIVER_CHAT = 222
OWNER_PROMPT_MSG = 51   # сообщение владельцу «завершил рейс» с кнопкой «Указать выручку»
DRIVER_PROMPT_MSG = 52  # сообщение водителю с кнопкой «Указать выручку»


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
    owner = Owner(telegram_id=OWNER_TG, notifications_enabled=True, full_name="Владелец")
    session.add(owner)
    await session.flush()
    driver = Driver(
        owner_id=owner.id, telegram_id=DRIVER_TG, full_name="тест",
        salary_type="per_km", salary_rate=Decimal(0),
    )
    vehicle = Vehicle(owner_id=owner.id, license_plate="А001АА")
    session.add_all([driver, vehicle])
    await session.flush()
    shift = Shift(
        owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id, status="started"
    )
    session.add(shift)
    await session.flush()
    trip = Trip(
        owner_id=owner.id, shift_id=shift.id, driver_id=driver.id,
        vehicle_id=vehicle.id, status="completed",
        origin="10.13", destination="Пулково Волхонка",
    )
    session.add(trip)
    await session.flush()
    # То, что пишет _do_end_trip: id сообщений с кнопками «Указать выручку».
    await log_event(
        session, owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
        trip_id=trip.id, event_type="trip_revenue_prompt",
        payload={
            "driver_chat_id": DRIVER_CHAT, "driver_msg_id": DRIVER_PROMPT_MSG,
            "owner_chat_id": OWNER_CHAT, "owner_msg_id": OWNER_PROMPT_MSG,
        },
    )
    await session.commit()
    return owner, driver, shift, trip


def _mock_bot():
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=777))
    bot.edit_message_reply_markup = AsyncMock()
    return bot


def _mock_call(data: str, user_id: int, chat_id: int):
    call = MagicMock()
    call.data = data
    call.from_user = SimpleNamespace(id=user_id)
    call.message.chat = SimpleNamespace(id=chat_id)
    call.message.answer = AsyncMock(
        return_value=SimpleNamespace(message_id=888, chat=SimpleNamespace(id=chat_id))
    )
    call.message.edit_reply_markup = AsyncMock()
    call.answer = AsyncMock()
    return call


def _mock_message(text: str, user_id: int, chat_id: int):
    message = MagicMock()
    message.text = text
    message.from_user = SimpleNamespace(id=user_id)
    message.chat = SimpleNamespace(id=chat_id)
    message.answer = AsyncMock(
        return_value=SimpleNamespace(message_id=889, chat=SimpleNamespace(id=chat_id))
    )
    return message


def _fsm(storage: MemoryStorage, user_id: int) -> FSMContext:
    return FSMContext(
        storage=storage,
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def _markup_callbacks(markup) -> set[str]:
    if markup is None:
        return set()
    return {btn.callback_data for row in markup.inline_keyboard for btn in row}


async def _driver_sends_amount(sessionmaker, storage, amount: str, owner_bot):
    """Водитель прислал сумму: ставим его FSM и зовём настоящий хендлер."""
    async with sessionmaker() as session:
        trip = (await session.execute(select(Trip))).scalars().first()
        state = _fsm(storage, DRIVER_TG)
        await state.set_state(DriverTripRevenue.waiting_for_amount)
        await state.update_data(trip_id=trip.id)
        message = _mock_message(amount, DRIVER_TG, DRIVER_CHAT)
        await driver_revenue_amount(message, state, session, owner_bot)
        return message, state


# =========================================================================
# Сценарий со скриншота: водитель указал 12000, владелец жмёт всё подряд
# =========================================================================
def test_screenshot_scenario_no_double_revenue(loop_run):
    async def scenario():
        engine, sessionmaker = await _make_db()
        storage = MemoryStorage()
        owner_bot = _mock_bot()   # бот владельца (мок Telegram)
        driver_bot = _mock_bot()  # бот водителя

        async with sessionmaker() as session:
            _, _, _, trip = await _seed(session)
            trip_id = trip.id

        # 1. Водитель прислал 12000 → pending, у владельца гаснет «Указать выручку».
        await _driver_sends_amount(sessionmaker, storage, "12000", owner_bot)
        async with sessionmaker() as session:
            trip = await session.get(Trip, trip_id)
            assert trip.driver_revenue_pending_rub == Decimal("12000")
            assert trip.revenue_rub is None
        owner_bot.edit_message_reply_markup.assert_any_call(
            chat_id=OWNER_CHAT, message_id=OWNER_PROMPT_MSG, reply_markup=None
        )
        # decision-сообщение записано в события
        async with sessionmaker() as session:
            ev = (
                await session.execute(
                    select(Event).where(Event.event_type == "trip_revenue_decision_prompt")
                )
            ).scalars().all()
            assert len(ev) == 1 and ev[0].payload["owner_msg_id"] == 777

        # 2. Владелец всё же жмёт «Указать выручку» (кнопка могла не успеть
        #    погаснуть) → ввод текстом НЕ открывается, только «Одобрить/Изменить».
        owner_state = _fsm(storage, OWNER_TG)
        async with sessionmaker() as session:
            call = _mock_call(f"trip:revenue:{trip_id}", OWNER_TG, OWNER_CHAT)
            await cb_trip_revenue_start(call, owner_state, session, _mock_bot())
        assert await owner_state.get_state() is None  # FSM ввода не запущен!
        sent_markup = call.message.answer.call_args.kwargs.get("reply_markup")
        assert _markup_callbacks(sent_markup) == {f"trev:ok:{trip_id}", f"trev:edit:{trip_id}"}

        # 3. Владелец жмёт «Одобрить» → выручка 12000, у водителя кнопка гаснет.
        async with sessionmaker() as session:
            call_ok = _mock_call(f"trev:ok:{trip_id}", OWNER_TG, OWNER_CHAT)
            await cb_trip_revenue_approve(call_ok, owner_state, session, driver_bot, owner_bot)
        async with sessionmaker() as session:
            trip = await session.get(Trip, trip_id)
            assert trip.revenue_rub == Decimal("12000")
            assert trip.driver_revenue_pending_rub is None
        driver_bot.edit_message_reply_markup.assert_any_call(
            chat_id=DRIVER_CHAT, message_id=DRIVER_PROMPT_MSG, reply_markup=None
        )

        # 4. Владелец печатает «10000» — раньше это молча перезаписывало выручку.
        #    Теперь FSM пуст, диспетчер не отдал бы сообщение в set_trip_revenue.
        assert await owner_state.get_state() is None
        async with sessionmaker() as session:
            trip = await session.get(Trip, trip_id)
            assert trip.revenue_rub == Decimal("12000")  # не 10000!
        await engine.dispose()

    loop_run(scenario())


# =========================================================================
# Владелец открыл ввод суммы, потом нажал «Одобрить» — FSM должен погаснуть
# =========================================================================
def test_approve_clears_pending_typing_state(loop_run):
    async def scenario():
        engine, sessionmaker = await _make_db()
        storage = MemoryStorage()
        owner_bot, driver_bot = _mock_bot(), _mock_bot()

        async with sessionmaker() as session:
            _, _, _, trip = await _seed(session)
            trip_id = trip.id
        await _driver_sends_amount(sessionmaker, storage, "12000", owner_bot)

        owner_state = _fsm(storage, OWNER_TG)
        await owner_state.set_state(SetTripRevenue.waiting_for_amount)
        await owner_state.update_data(trip_id=trip_id)

        async with sessionmaker() as session:
            call_ok = _mock_call(f"trev:ok:{trip_id}", OWNER_TG, OWNER_CHAT)
            await cb_trip_revenue_approve(call_ok, owner_state, session, driver_bot, owner_bot)

        assert await owner_state.get_state() is None
        async with sessionmaker() as session:
            trip = await session.get(Trip, trip_id)
            assert trip.revenue_rub == Decimal("12000")
        await engine.dispose()

    loop_run(scenario())


# =========================================================================
# Двойной клик «Одобрить»: второй раз — алерт, без дублей и перезаписи
# =========================================================================
def test_double_approve_is_idempotent(loop_run):
    async def scenario():
        engine, sessionmaker = await _make_db()
        storage = MemoryStorage()
        owner_bot, driver_bot = _mock_bot(), _mock_bot()

        async with sessionmaker() as session:
            _, _, _, trip = await _seed(session)
            trip_id = trip.id
        await _driver_sends_amount(sessionmaker, storage, "12000", owner_bot)

        owner_state = _fsm(storage, OWNER_TG)
        async with sessionmaker() as session:
            call1 = _mock_call(f"trev:ok:{trip_id}", OWNER_TG, OWNER_CHAT)
            await cb_trip_revenue_approve(call1, owner_state, session, driver_bot, owner_bot)
        assert call1.message.answer.call_count == 1  # «Выручка принята»

        async with sessionmaker() as session:
            call2 = _mock_call(f"trev:ok:{trip_id}", OWNER_TG, OWNER_CHAT)
            await cb_trip_revenue_approve(call2, owner_state, session, driver_bot, owner_bot)
        call2.message.answer.assert_not_called()  # дубля «Выручка принята» нет
        alert_text = call2.answer.call_args.args[0]
        assert "уже принята" in alert_text
        async with sessionmaker() as session:
            trip = await session.get(Trip, trip_id)
            assert trip.revenue_rub == Decimal("12000")
        await engine.dispose()

    loop_run(scenario())


# =========================================================================
# Владелец указал первым → у водителя кнопка гаснет, его клик — алерт
# =========================================================================
def test_owner_first_disables_driver_button(loop_run):
    async def scenario():
        engine, sessionmaker = await _make_db()
        storage = MemoryStorage()
        owner_bot, driver_bot = _mock_bot(), _mock_bot()

        async with sessionmaker() as session:
            _, _, _, trip = await _seed(session)
            trip_id = trip.id

        # Владелец открывает ввод (водитель ещё ничего не прислал) и вводит 5000.
        owner_state = _fsm(storage, OWNER_TG)
        async with sessionmaker() as session:
            call = _mock_call(f"trip:revenue:{trip_id}", OWNER_TG, OWNER_CHAT)
            await cb_trip_revenue_start(call, owner_state, session, owner_bot)
        assert await owner_state.get_state() == SetTripRevenue.waiting_for_amount.state

        async with sessionmaker() as session:
            message = _mock_message("5000", OWNER_TG, OWNER_CHAT)
            await set_trip_revenue(message, owner_state, session, driver_bot, owner_bot)
        assert await owner_state.get_state() is None
        async with sessionmaker() as session:
            trip = await session.get(Trip, trip_id)
            assert trip.revenue_rub == Decimal("5000")
        # у водителя кнопка «Указать выручку» погашена
        driver_bot.edit_message_reply_markup.assert_any_call(
            chat_id=DRIVER_CHAT, message_id=DRIVER_PROMPT_MSG, reply_markup=None
        )

        # Водитель всё же жмёт свою кнопку → алерт, FSM не открывается.
        driver_state = _fsm(storage, DRIVER_TG)
        async with sessionmaker() as session:
            dcall = _mock_call(f"drev:{trip_id}", DRIVER_TG, DRIVER_CHAT)
            await cb_driver_revenue(dcall, driver_state, session)
        assert await driver_state.get_state() is None
        alert_text = dcall.answer.call_args.args[0]
        assert "уже указана" in alert_text
        await engine.dispose()

    loop_run(scenario())


# =========================================================================
# «Изменить»: владелец правит сумму водителя — висящие кнопки гаснут
# =========================================================================
def test_edit_flow_closes_all_buttons(loop_run):
    async def scenario():
        engine, sessionmaker = await _make_db()
        storage = MemoryStorage()
        owner_bot, driver_bot = _mock_bot(), _mock_bot()

        async with sessionmaker() as session:
            _, _, _, trip = await _seed(session)
            trip_id = trip.id
        await _driver_sends_amount(sessionmaker, storage, "12000", owner_bot)

        # «Изменить» открывает ввод…
        owner_state = _fsm(storage, OWNER_TG)
        async with sessionmaker() as session:
            call = _mock_call(f"trev:edit:{trip_id}", OWNER_TG, OWNER_CHAT)
            await cb_trip_revenue_edit(call, owner_state, session)
        assert await owner_state.get_state() == SetTripRevenue.waiting_for_amount.state

        # …владелец вводит финальные 9000: pending закрыт, кнопки погашены.
        final_owner_bot = _mock_bot()
        async with sessionmaker() as session:
            message = _mock_message("9000", OWNER_TG, OWNER_CHAT)
            await set_trip_revenue(message, owner_state, session, driver_bot, final_owner_bot)
        async with sessionmaker() as session:
            trip = await session.get(Trip, trip_id)
            assert trip.revenue_rub == Decimal("9000")
            assert trip.driver_revenue_pending_rub is None
        # у водителя погасла «Указать выручку», у владельца — «Одобрить/Изменить»
        driver_bot.edit_message_reply_markup.assert_any_call(
            chat_id=DRIVER_CHAT, message_id=DRIVER_PROMPT_MSG, reply_markup=None
        )
        final_owner_bot.edit_message_reply_markup.assert_any_call(
            chat_id=OWNER_CHAT, message_id=777, reply_markup=None
        )
        await engine.dispose()

    loop_run(scenario())


# =========================================================================
# Правило первого на уровне БД: второй pending не перетирает первый
# =========================================================================
def test_pending_is_write_once(loop_run):
    async def scenario():
        engine, sessionmaker = await _make_db()
        async with sessionmaker() as session:
            _, _, _, trip = await _seed(session)
            assert await trip_service.set_trip_driver_revenue_pending(
                session, trip=trip, revenue_rub=Decimal("12000")
            )
            await session.commit()
            assert not await trip_service.set_trip_driver_revenue_pending(
                session, trip=trip, revenue_rub=Decimal("99999")
            )
            await session.commit()
            await session.refresh(trip)
            assert trip.driver_revenue_pending_rub == Decimal("12000")
        await engine.dispose()

    loop_run(scenario())


# =========================================================================
# Кнопки меню не съедаются шагами FSM (фильтр ~F.text.in_(ALL_DRIVER_BUTTONS))
# =========================================================================
def _handler(name: str):
    for h in driver_router.message.handlers:
        if h.callback.__name__ == name:
            return h
    raise AssertionError(f"handler {name} не найден")


@pytest.mark.parametrize(
    "handler_name,state",
    [
        ("driver_revenue_amount", DriverTripRevenue.waiting_for_amount),
        ("trip_origin", NewTrip.waiting_for_origin),
        ("trip_destination", NewTrip.waiting_for_destination),
    ],
)
def test_menu_buttons_escape_fsm_traps(loop_run, handler_name, state):
    async def scenario():
        h = _handler(handler_name)
        for btn in (kb.BTN_EXPENSE, kb.BTN_SOS, kb.BTN_END_TRIP, kb.BTN_ADD_TRIP):
            event = SimpleNamespace(text=btn)
            ok, _ = await h.check(event, raw_state=state.state)
            assert not ok, f"{handler_name} не должен съедать кнопку {btn!r}"
        # обычный текст по-прежнему обрабатывается этим шагом
        ok, _ = await h.check(SimpleNamespace(text="12000"), raw_state=state.state)
        assert ok

    loop_run(scenario())


# =========================================================================
# Анти-даблклик: апдейты одного пользователя идут строго по очереди
# =========================================================================
def test_per_user_lock_serializes_same_user(loop_run):
    from app.bots.middlewares import PerUserLockMiddleware

    async def scenario():
        mw = PerUserLockMiddleware()
        running: list[str] = []

        async def slow_handler(event, data):
            running.append(f"start:{event.tag}")
            await asyncio.sleep(0.05)
            running.append(f"end:{event.tag}")

        same_user = SimpleNamespace(from_user=SimpleNamespace(id=1))
        e1 = SimpleNamespace(from_user=same_user.from_user, tag="first")
        e2 = SimpleNamespace(from_user=same_user.from_user, tag="second")
        await asyncio.gather(mw(slow_handler, e1, {}), mw(slow_handler, e2, {}))
        # второй апдейт НЕ начался, пока не закончился первый
        assert running == ["start:first", "end:first", "start:second", "end:second"]

        # разные пользователи — параллельно
        running.clear()
        e3 = SimpleNamespace(from_user=SimpleNamespace(id=2), tag="a")
        e4 = SimpleNamespace(from_user=SimpleNamespace(id=3), tag="b")
        await asyncio.gather(mw(slow_handler, e3, {}), mw(slow_handler, e4, {}))
        assert running[:2] == ["start:a", "start:b"]  # оба стартовали сразу

    loop_run(scenario())
