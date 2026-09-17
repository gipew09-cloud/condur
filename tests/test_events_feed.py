"""Лента событий владельца — то, что в приложении открывает кнопка «Журнал».

Владелец 08.09.2026: «слева я бы хотел там сообщение логи все что происходило
как в Telegram даже лучше».

Проверяем две вещи, на которых такая лента обычно и ломается: в неё не должен
попадать служебный шум, и машина должна определяться по смене или рейсу, а не
«по времени» — водитель за день пересаживается.
"""
import asyncio
import os
from types import SimpleNamespace
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

from fastapi import HTTPException  # noqa: E402

from app.models import (  # noqa: E402
    Base, DistributionCenter, Driver, Event, Expense, Owner, Shift, Trip, Vehicle,
)
from app.web.router import (  # noqa: E402
    api_events, api_expense_decision, shift_delete,
)

NOW = datetime.now(timezone.utc)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


# Движки, поднятые сценариями. ⚠️ Их обязательно гасить: соединение aiosqlite
# живёт в отдельном потоке, и если тест упал до session.close(), поток остаётся,
# а pytest виснет на выходе — вместо ошибки видно пустой экран и тишину.
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
    vehicle = Vehicle(owner_id=owner.id, license_plate="Т557ОС178", is_active=True)
    driver = Driver(owner_id=owner.id, full_name="Саломов", telegram_id=2)
    session.add_all([vehicle, driver])
    await session.flush()
    return session, owner, vehicle, driver


def test_служебный_шум_в_ленту_не_попадает():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        session.add_all([
            Event(owner_id=owner.id, driver_id=driver.id,
                  event_type="waybill_uploaded", created_at=NOW - timedelta(hours=1)),
            # напоминания и опросы — не события, а рассылки
            Event(owner_id=owner.id, driver_id=driver.id,
                  event_type="start_shift_reminder", created_at=NOW - timedelta(hours=2)),
            Event(owner_id=owner.id, driver_id=driver.id,
                  event_type="trip_revenue_prompt", created_at=NOW - timedelta(hours=3)),
            Event(owner_id=owner.id, driver_id=driver.id,
                  event_type="location_sent", created_at=NOW - timedelta(hours=4)),
        ])
        await session.commit()

        data = await api_events(owner, session)
        types = [e["type"] for e in data["events"]]
        assert types == ["waybill_uploaded"]
        assert data["events"][0]["label"] == "Фото ТТН"
        await session.close()
    _run(scenario)


def test_машина_берётся_из_смены_а_не_угадывается():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        shift = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                      started_at=NOW - timedelta(hours=5))
        session.add(shift)
        await session.flush()
        session.add_all([
            Event(owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
                  event_type="shift_started", created_at=NOW - timedelta(hours=5)),
            # событие без смены и рейса — машину знать неоткуда
            Event(owner_id=owner.id, driver_id=driver.id,
                  event_type="sos", created_at=NOW - timedelta(hours=1)),
        ])
        await session.commit()

        data = await api_events(owner, session)
        by_type = {e["type"]: e for e in data["events"]}
        assert by_type["shift_started"]["plate"] == "Т557ОС178"
        assert by_type["shift_started"]["driver"] == "Саломов"
        # ⚠️ Не выдумываем: нет связи — нет машины.
        assert by_type["sos"]["plate"] is None
        await session.close()
    _run(scenario)


def test_свежие_события_сверху_и_старьё_не_тянем():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        session.add_all([
            Event(owner_id=owner.id, driver_id=driver.id, event_type="sos",
                  created_at=NOW - timedelta(hours=1)),
            Event(owner_id=owner.id, driver_id=driver.id, event_type="cash_submitted",
                  payload={"amount": "12000"}, created_at=NOW - timedelta(hours=10)),
            Event(owner_id=owner.id, driver_id=driver.id, event_type="expense_submitted",
                  created_at=NOW - timedelta(days=20)),
        ])
        await session.commit()

        data = await api_events(owner, session, hours=48)
        types = [e["type"] for e in data["events"]]
        assert types == ["sos", "cash_submitted"]
        await session.close()
    _run(scenario)


def test_у_расхода_видно_категорию_сумму_и_чек():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        expense = Expense(
            owner_id=owner.id, driver_id=driver.id, category="fuel",
            amount_rub=Decimal("4500"), status="pending",
            receipt_photo_url="AgACAgIAA-чек",
        )
        session.add(expense)
        await session.flush()
        session.add(Event(
            owner_id=owner.id, driver_id=driver.id, event_type="expense_submitted",
            payload={"expense_id": expense.id, "category": "fuel", "amount": "4500"},
            created_at=NOW - timedelta(hours=1),
        ))
        await session.commit()

        row = (await api_events(owner, session))["events"][0]
        assert row["detail"] == "Топливо · 4 500 ₽"
        assert row["photo"] == "AgACAgIAA-чек"
        # Решения по нему ещё нет — в приложении такая строка помечена.
        assert row["awaiting"] is True
        assert row["expense_id"] == expense.id
        await session.close()
    _run(scenario)


def test_ноль_и_мусор_в_сумме_это_не_сумма():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        session.add_all([
            Event(owner_id=owner.id, driver_id=driver.id, event_type="cash_submitted",
                  payload={"amount": "0"}, created_at=NOW - timedelta(hours=1)),
            Event(owner_id=owner.id, driver_id=driver.id, event_type="trip_revenue_set",
                  payload={"revenue": "не помню"}, created_at=NOW - timedelta(hours=2)),
        ])
        await session.commit()

        details = [e["detail"] for e in (await api_events(owner, session))["events"]]
        # ⚠️ «0 ₽» в ленте читается как «сдал ноль». На деле суммы просто нет.
        assert details == [None, None]
        await session.close()
    _run(scenario)


def test_место_есть_только_у_событий_геозоны():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        rc = DistributionCenter(
            owner_id=owner.id, name="Пятёрочка Шушары", address="Шушары",
            latitude=Decimal("59.8100000"), longitude=Decimal("30.4000000"),
        )
        session.add(rc)
        await session.flush()
        session.add_all([
            Event(owner_id=owner.id, driver_id=driver.id, event_type="rc_departed",
                  payload={"rc_id": rc.id, "rc_name": "Пятёрочка Шушары",
                           "waited_minutes": 88},
                  created_at=NOW - timedelta(hours=1)),
            Event(owner_id=owner.id, driver_id=driver.id, event_type="sos",
                  payload={"state": "в рейсе"}, created_at=NOW - timedelta(hours=2)),
        ])
        await session.commit()

        by_type = {e["type"]: e for e in (await api_events(owner, session))["events"]}
        zone_row = by_type["rc_departed"]
        assert zone_row["place"] == "Пятёрочка Шушары"
        assert (zone_row["lat"], zone_row["lon"]) == (59.81, 30.4)
        assert zone_row["detail"] == "Пятёрочка Шушары · стоял 1 ч 28 мин"
        # ⚠️ У SOS координат нет и взяться им неоткуда — на карте его не будет.
        assert by_type["sos"]["lat"] is None and by_type["sos"]["lon"] is None
        assert by_type["sos"]["detail"] == "в рейсе"
        await session.close()
    _run(scenario)


def test_чужой_расход_по_id_из_payload_не_подтягивается():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        stranger = Owner(telegram_id=99, full_name="Чужой", timezone="Europe/Moscow")
        session.add(stranger)
        await session.flush()
        stranger_driver = Driver(owner_id=stranger.id, full_name="Чужой водитель")
        session.add(stranger_driver)
        await session.flush()
        alien = Expense(
            owner_id=stranger.id, driver_id=stranger_driver.id, category="repair",
            amount_rub=Decimal("99000"), status="pending",
            receipt_photo_url="AgACAgIAA-чужой",
        )
        session.add(alien)
        await session.flush()
        session.add(Event(
            owner_id=owner.id, driver_id=driver.id, event_type="expense_submitted",
            payload={"expense_id": alien.id}, created_at=NOW - timedelta(hours=1),
        ))
        await session.commit()

        row = (await api_events(owner, session))["events"][0]
        assert row["expense_id"] is None
        assert row["photo"] is None
        assert row["detail"] is None
        await session.close()
    _run(scenario)


def test_день_считается_в_поясе_владельца_а_не_в_utc():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        # 21:30 по Москве 9 сентября — это ещё 18:30 UTC того же дня, но в
        # поясе Владивостока уже 10-е. День берём по поясу владельца.
        moscow_evening = datetime(2026, 9, 9, 18, 30, tzinfo=timezone.utc)
        session.add(Event(
            owner_id=owner.id, driver_id=driver.id, event_type="cash_submitted",
            payload={"amount": "12000"}, created_at=moscow_evening,
        ))
        await session.commit()

        row = (await api_events(owner, session, hours=24 * 30))["events"][0]
        assert row["day"] == "2026-09-09"
        assert row["time_label"] == "21:30"
        assert row["day_label"].endswith("сентября") or row["day_label"] in (
            "Сегодня", "Вчера",
        )
        await session.close()
    _run(scenario)


# Запрос нужен эндпоинту только ради бота водителя, а сообщение водителю идёт
# по выключенному по умолчанию флагу — в тестах достаточно заглушки.
_REQUEST = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(driver_bot=None)))


async def _expense(session, owner, driver, amount="4500", status="pending"):
    expense = Expense(
        owner_id=owner.id, driver_id=driver.id, category="fuel",
        amount_rub=Decimal(amount), status=status,
    )
    session.add(expense)
    await session.flush()
    session.add(Event(
        owner_id=owner.id, driver_id=driver.id, event_type="expense_submitted",
        payload={"expense_id": expense.id, "category": "fuel", "amount": amount},
        created_at=NOW - timedelta(hours=1),
    ))
    await session.commit()
    return expense


def test_расход_одобряется_из_приложения_и_это_видно_в_ленте():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        expense = await _expense(session, owner, driver)

        answer = await api_expense_decision(
            _REQUEST, expense.id, owner, session, action="approve",
        )
        assert answer["status"] == "approved"
        assert answer["already_decided"] is False
        assert expense.decided_at is not None

        rows = (await api_events(owner, session))["events"]
        # Решение попадает в ленту: журнал должен показывать и то, что сделал
        # сам владелец, иначе завтра не вспомнить, кто одобрил.
        assert rows[0]["label"] == "Расход утверждён"
        # А исходная строка расхода больше никого не ждёт.
        assert rows[1]["awaiting"] is False
        await session.close()
    _run(scenario)


def test_повторное_нажатие_не_переигрывает_решение():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        expense = await _expense(session, owner, driver)

        await api_expense_decision(_REQUEST, expense.id, owner, session, action="approve")
        again = await api_expense_decision(
            _REQUEST, expense.id, owner, session, action="reject",
        )
        # ⚠️ В дороге связь рвётся, и второе нажатие не должно отменять первое.
        assert again["status"] == "approved"
        assert again["already_decided"] is True

        labels = [e["label"] for e in (await api_events(owner, session))["events"]]
        assert labels.count("Расход утверждён") == 1
        assert "Расход отклонён" not in labels
        await session.close()
    _run(scenario)


def test_исправленная_сумма_вписывается_до_решения():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        expense = await _expense(session, owner, driver, amount="4500")

        answer = await api_expense_decision(
            _REQUEST, expense.id, owner, session,
            action="approve", amount_rub="4 100,50",
        )
        assert answer["amount_rub"] == "4100.50"
        assert expense.amount_rub == Decimal("4100.50")

        rows = {e["label"]: e for e in (await api_events(owner, session))["events"]}
        assert rows["Сумма расхода исправлена"]["detail"] == "Топливо · 4 100 ₽"
        # Одобрено уже исправленное, а не то, что прислал водитель.
        assert rows["Расход утверждён"]["detail"] == "Топливо · 4 100 ₽"
        await session.close()
    _run(scenario)


def test_отказ_виден_в_ленте_отдельной_строкой():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        expense = await _expense(session, owner, driver)

        answer = await api_expense_decision(
            _REQUEST, expense.id, owner, session, action="reject",
        )
        assert answer["status"] == "rejected"
        labels = [e["label"] for e in (await api_events(owner, session))["events"]]
        assert labels[0] == "Расход отклонён"
        await session.close()
    _run(scenario)


def test_чужой_расход_решать_нельзя():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        stranger = Owner(telegram_id=98, full_name="Чужой", timezone="Europe/Moscow")
        session.add(stranger)
        await session.flush()
        stranger_driver = Driver(owner_id=stranger.id, full_name="Чужой водитель")
        session.add(stranger_driver)
        await session.flush()
        alien = await _expense(session, stranger, stranger_driver)

        with pytest.raises(HTTPException) as failed:
            await api_expense_decision(
                _REQUEST, alien.id, owner, session, action="approve",
            )
        assert failed.value.status_code == 404
        assert alien.status == "pending"
        await session.close()
    _run(scenario)


def test_мусор_вместо_суммы_не_проходит():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        expense = await _expense(session, owner, driver)

        with pytest.raises(HTTPException) as failed:
            await api_expense_decision(
                _REQUEST, expense.id, owner, session,
                action="approve", amount_rub="не помню",
            )
        assert failed.value.status_code == 400
        # ⚠️ Расход остаётся нерешённым: полдела делать нельзя.
        assert expense.status == "pending"
        await session.close()
    _run(scenario)


def test_приезд_на_рц_показывается_временем_приезда_а_не_обнаружения():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        rc = DistributionCenter(
            owner_id=owner.id, name="Пятёрочка Шушары", address="Шушары",
            latitude=Decimal("59.81"), longitude=Decimal("30.4"),
        )
        session.add(rc)
        await session.flush()
        # Машина встала в 17:12, сервер заметил это в 17:19: приезд считается
        # только после 4 минут стоянки, а проверка крутится раз в 5 минут.
        parked = NOW - timedelta(hours=2)
        noticed = parked + timedelta(minutes=7)
        session.add(Event(
            owner_id=owner.id, driver_id=driver.id, event_type="rc_arrived",
            payload={"rc_id": rc.id, "rc_name": "Пятёрочка Шушары",
                     "parked_since": parked.isoformat()},
            created_at=noticed,
        ))
        await session.commit()

        row = (await api_events(owner, session))["events"][0]
        # ⚠️ Владелец 10.09.2026: «правильно будет, когда она приехала».
        assert row["at"].startswith(parked.isoformat()[:16])
        assert row["time_label"] != row["seen_at_label"]
        # Но и второе время не прячем: в споре нужны оба.
        assert row["seen_at_label"] is not None
        await session.close()
    _run(scenario)


def test_у_обычного_события_второго_времени_нет():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        session.add(Event(
            owner_id=owner.id, driver_id=driver.id, event_type="cash_submitted",
            payload={"amount": "500"}, created_at=NOW - timedelta(hours=1),
        ))
        await session.commit()

        row = (await api_events(owner, session))["events"][0]
        # Кнопку водитель нажал сам — время записи и есть время события.
        assert row["seen_at_label"] is None
        await session.close()
    _run(scenario)


def test_мусор_в_payload_не_двигает_время_события():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        session.add_all([
            Event(owner_id=owner.id, driver_id=driver.id, event_type="rc_arrived",
                  payload={"parked_since": "позавчера"},
                  created_at=NOW - timedelta(hours=1)),
            Event(owner_id=owner.id, driver_id=driver.id, event_type="rc_arrived",
                  payload={"parked_since": (NOW + timedelta(hours=5)).isoformat()},
                  created_at=NOW - timedelta(hours=2)),
        ])
        await session.commit()

        rows = (await api_events(owner, session))["events"]
        # ⚠️ Ни строка-бессмыслица, ни время из будущего не должны попадать в
        # ленту: остаётся время записи.
        for row in rows:
            assert row["seen_at_label"] is None
        await session.close()
    _run(scenario)


def test_лента_отсортирована_по_настоящему_времени():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        base = NOW - timedelta(hours=3)
        session.add_all([
            # Приезд случился раньше, а записан позже соседнего события.
            Event(owner_id=owner.id, driver_id=driver.id, event_type="rc_arrived",
                  payload={"parked_since": base.isoformat()},
                  created_at=base + timedelta(minutes=8)),
            Event(owner_id=owner.id, driver_id=driver.id, event_type="cash_submitted",
                  payload={"amount": "700"}, created_at=base + timedelta(minutes=4)),
        ])
        await session.commit()

        types = [e["type"] for e in (await api_events(owner, session))["events"]]
        assert types == ["cash_submitted", "rc_arrived"]
        await session.close()
    _run(scenario)


def test_у_подмены_машины_видно_обе_машины():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        other = Vehicle(owner_id=owner.id, license_plate="В123АА47", is_active=True)
        session.add(other)
        await session.flush()
        session.add(Event(
            owner_id=owner.id, driver_id=driver.id,
            event_type="vehicle_mixup_alert",
            payload={"shift_vehicle_id": vehicle.id, "moving_vehicle_id": other.id},
            created_at=NOW - timedelta(hours=1),
        ))
        await session.commit()

        row = (await api_events(owner, session))["events"][0]
        # «Необычная машина» ничего не объясняла — теперь видно, в чём дело.
        assert row["label"] == "Возможно, не та машина"
        assert row["detail"] == "Т557ОС178 стоит в смене · В123АА47 едет без смены"
        await session.close()
    _run(scenario)


def test_удалённая_смена_исчезает_и_из_журнала():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        shift = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                      started_at=NOW - timedelta(hours=2), status="started")
        session.add(shift)
        await session.flush()
        expense = Expense(
            owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
            category="fuel", amount_rub=Decimal("3000"), status="pending",
        )
        session.add(expense)
        session.add_all([
            Event(owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
                  event_type="shift_started", created_at=NOW - timedelta(hours=2)),
            # Чек — это настоящий документ, он остаётся.
            Event(owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
                  event_type="expense_submitted",
                  payload={"category": "fuel", "amount": "3000"},
                  created_at=NOW - timedelta(hours=1)),
        ])
        await session.commit()

        await shift_delete(shift.id, owner, session)

        types = [e["type"] for e in (await api_events(owner, session))["events"]]
        # ⚠️ Владелец 10.09.2026: «удалил смену — а в приложении она всё равно есть».
        assert "shift_started" not in types
        # А расход остался: чек никуда не делся.
        assert "expense_submitted" in types
        assert await session.get(Shift, shift.id) is None
        await session.close()
    _run(scenario)


def test_удаление_смены_убирает_и_записи_её_рейсов():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        shift = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                      started_at=NOW - timedelta(hours=3), status="started")
        session.add(shift)
        await session.flush()
        trip = Trip(owner_id=owner.id, shift_id=shift.id, driver_id=driver.id,
                    vehicle_id=vehicle.id, status="in_transit",
                    origin="Агропарк", destination="Шушары")
        session.add(trip)
        await session.flush()
        session.add_all([
            Event(owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
                  trip_id=trip.id, event_type="trip_in_transit",
                  created_at=NOW - timedelta(hours=2)),
            Event(owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
                  trip_id=trip.id, event_type="waybill_uploaded",
                  created_at=NOW - timedelta(hours=1)),
        ])
        await session.commit()

        await shift_delete(shift.id, owner, session)

        types = [e["type"] for e in (await api_events(owner, session))["events"]]
        assert "trip_in_transit" not in types
        # Фото ТТН — снимок, он остаётся в журнале и без рейса.
        assert "waybill_uploaded" in types
        await session.close()
    _run(scenario)


def test_старые_сироты_удалённых_смен_в_ленту_не_попадают():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        # Так выглядят записи, оставшиеся от смены, удалённой ДО того, как
        # появилась уборка: тип есть, а ссылки на смену уже нет.
        session.add_all([
            Event(owner_id=owner.id, driver_id=driver.id,
                  event_type="shift_started", created_at=NOW - timedelta(hours=5)),
            Event(owner_id=owner.id, driver_id=driver.id,
                  event_type="trip_completed", created_at=NOW - timedelta(hours=4)),
            # А это не сирота: SOS и не должен быть привязан к смене.
            Event(owner_id=owner.id, driver_id=driver.id,
                  event_type="sos", payload={"state": "в рейсе"},
                  created_at=NOW - timedelta(hours=3)),
        ])
        await session.commit()

        types = [e["type"] for e in (await api_events(owner, session))["events"]]
        # ⚠️ Владелец 11.09.2026: «удалил смену на сайте, а она у меня до сих
        # пор есть 10 сентября».
        assert types == ["sos"]
        await session.close()
    _run(scenario)


def test_живая_смена_из_ленты_не_пропадает():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        shift = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                      started_at=NOW - timedelta(hours=2), status="started")
        session.add(shift)
        await session.flush()
        session.add(Event(
            owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
            event_type="shift_started", created_at=NOW - timedelta(hours=2),
        ))
        await session.commit()

        rows = (await api_events(owner, session))["events"]
        assert [e["type"] for e in rows] == ["shift_started"]
        assert rows[0]["plate"] == "Т557ОС178"
        await session.close()
    _run(scenario)


def test_тишина_водителя_не_выдаётся_за_потерю_сигнала():
    """Владелец 12.09.2026: «что за баг, почему пропал сигнал, если на экране
    всё видно» и «что за 4.0 или 4.4 часа, почему нельзя нормально».

    Трекер в этот момент шлёт точки — молчит не он, а водитель с открытой
    сменой. И длительность читается человеком, а не пересчитывается из
    десятых долей часа.
    """
    async def scenario():
        session, owner, vehicle, driver = await _db()
        session.add(Event(
            owner_id=owner.id, driver_id=driver.id,
            event_type="silence_alert", created_at=NOW - timedelta(minutes=30),
            payload={"hours_silent": 4.4, "minutes_silent": 264},
        ))
        await session.commit()

        data = await api_events(owner, session)
        row = data["events"][0]
        assert row["label"] == "Водитель не выходит на связь"
        assert "сигнал" not in row["label"].lower()
        assert row["detail"] == "нет вестей 4 ч 24 мин"
        await session.close()
    _run(scenario)


def test_у_старой_тишины_часы_превращаются_в_минуты():
    """Записи до 12.09.2026 знают только «4.0 ч» — строка всё равно должна
    читаться так же, иначе в одной ленте два разных языка."""
    async def scenario():
        session, owner, vehicle, driver = await _db()
        session.add(Event(
            owner_id=owner.id, driver_id=driver.id,
            event_type="silence_alert", created_at=NOW - timedelta(hours=1),
            payload={"hours_silent": 4.0},
        ))
        await session.commit()

        data = await api_events(owner, session)
        assert data["events"][0]["detail"] == "нет вестей 4 ч"
        await session.close()
    _run(scenario)


def test_в_журнале_те_же_фото_что_в_telegram():
    """Владелец 17.09.2026: «в приложении должны быть показаны все
    фотографии, которые присылаются в Telegram». Одометр в начале и в конце
    смены раньше в журнал не попадал."""
    async def scenario():
        session, owner, vehicle, driver = await _db()
        shift = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                      started_at=NOW - timedelta(hours=5),
                      odometer_start_photo_url="AgAC-одометр-утро",
                      odometer_end_photo_url="app-7")
        session.add(shift)
        await session.flush()
        expense = await _expense(session, owner, driver)
        expense.receipt_photo_url = "AgAC-чек"
        session.add_all([
            Event(owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
                  event_type="shift_started", created_at=NOW - timedelta(hours=5)),
            Event(owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
                  event_type="shift_completed", created_at=NOW - timedelta(hours=1)),
            Event(owner_id=owner.id, driver_id=driver.id,
                  event_type="expense_rejected", created_at=NOW - timedelta(minutes=30),
                  payload={"expense_id": expense.id}),
        ])
        await session.commit()

        rows = {e["type"]: e for e in (await api_events(owner, session))["events"]}
        assert rows["shift_started"]["photo"] == "AgAC-одометр-утро"
        assert rows["shift_completed"]["photo"] == "app-7"
        assert rows["expense_rejected"]["photo"] == "AgAC-чек"
        await session.close()
    _run(scenario)


def test_смена_без_фото_в_журнале_без_фото():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        shift = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                      started_at=NOW - timedelta(hours=5))
        session.add(shift)
        await session.flush()
        session.add(Event(owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
                          event_type="shift_started", created_at=NOW - timedelta(hours=5)))
        await session.commit()
        row = (await api_events(owner, session))["events"][0]
        assert row["photo"] is None
        assert row["plate"] == "Т557ОС178"
        await session.close()
    _run(scenario)
