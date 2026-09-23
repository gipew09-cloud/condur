"""Книга денег: одна запись расхода и много окон на неё.

Владелец 23.09.2026: «расходы же ещё должны быть в рейсах и сменах, поэтому
у нас тут всё запутано». Эти тесты держат правило, которое снимает путаницу:
трата записывается один раз, а рейс, смена и машина видят ту же запись.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("aiosqlite")

from sqlalchemy import BigInteger, event, func, select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from app.models import (  # noqa: E402
    Base, Driver, Expense, ExpenseAttachment, ManualEntry, Owner, Shift, Trip, Vehicle,
)
from app.services import finance_ledger as fl  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint(type_, compiler, **kw):
    return "INTEGER"


NOW = datetime.now(timezone.utc).replace(microsecond=0)
TODAY = NOW.astimezone(ZoneInfo("Europe/Moscow")).date()
PERIOD = fl.ExpenseFilter(date_from=TODAY - timedelta(days=30), date_to=TODAY + timedelta(days=1))


def _scenario(body):
    """Своя база на каждый тест. SQLite учим постгресовой функции timezone()."""
    async def run():
        engine = create_async_engine("sqlite+aiosqlite://")

        @event.listens_for(engine.sync_engine, "connect")
        def _teach(conn, _):
            def tz(zone, moment):
                if moment is None:
                    return None
                when = datetime.fromisoformat(str(moment))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return when.astimezone(ZoneInfo(str(zone))).strftime("%Y-%m-%d %H:%M:%S")
            conn.create_function("timezone", 2, tz)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            owner = Owner(telegram_id=1, full_name="Владелец")
            stranger = Owner(telegram_id=2, full_name="Чужой")
            session.add_all([owner, stranger])
            await session.flush()
            truck = Vehicle(owner_id=owner.id, license_plate="Т772НХ178")
            other_truck = Vehicle(owner_id=owner.id, license_plate="У774ЕТ178")
            foreign = Vehicle(owner_id=stranger.id, license_plate="А001АА78")
            session.add_all([truck, other_truck, foreign])
            await session.flush()
            driver = Driver(owner_id=owner.id, telegram_id=100, full_name="Саломов",
                            salary_type="per_km", salary_rate=Decimal(10))
            session.add(driver)
            await session.flush()
            shift = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=truck.id,
                          status="started", started_at=NOW - timedelta(hours=3))
            session.add(shift)
            await session.flush()
            trip = Trip(owner_id=owner.id, driver_id=driver.id, vehicle_id=truck.id,
                        shift_id=shift.id, origin="Агропарк", destination="Магнит",
                        status="completed", completed_at=NOW - timedelta(hours=1),
                        revenue_rub=Decimal("18000"))
            session.add(trip)
            await session.commit()
            ctx = dict(owner=owner, stranger=stranger, truck=truck, other_truck=other_truck,
                       foreign=foreign, driver=driver, shift=shift, trip=trip)
            await body(session, ctx)
        await engine.dispose()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


def test_несколько_трат_одним_документом_с_машиной_и_вложениями():
    async def body(s, c):
        created = await fl.create_expenses(
            s, owner_id=c["owner"].id,
            items=[
                fl.NewExpense("fuel", Decimal("12400.50"), None, [
                    fl.NewAttachment("photo", "чек.jpg", "image/jpeg", b"\xff\xd8jpeg"),
                ]),
                fl.NewExpense("wash", Decimal("700"), "мойка на Софийской", [
                    fl.NewAttachment("file", "акт.pdf", "application/pdf", b"%PDF-1.4"),
                ]),
            ],
            spent_at=NOW - timedelta(minutes=30), vehicle_id=c["truck"].id,
            payment_method="fuel_card", supplier="Лукойл",
        )
        await s.commit()
        assert len(created) == 2
        # Одна «бумага» — один номер документа у обеих трат.
        assert created[0].batch_id and created[0].batch_id == created[1].batch_id
        # Владелец сам себе не отказывает: сразу одобрено, водителя нет.
        assert all(e.status == "approved" and e.driver_id is None for e in created)
        assert all(e.created_by == "owner" for e in created)

        rows = await fl.expense_rows(s, c["owner"].id, "Europe/Moscow", PERIOD)
        assert {r.amount for r in rows} == {Decimal("12400.50"), Decimal("700")}
        fuel = next(r for r in rows if r.category == "fuel")
        assert fuel.vehicle == "Т772НХ178"
        assert fuel.payment == "Топливная карта"
        assert fuel.photos == 1 and fuel.files == 0 and fuel.has_receipt
        wash = next(r for r in rows if r.category == "wash")
        assert wash.files == 1 and wash.attachments[0]["filename"] == "акт.pdf"
    _scenario(body)


def test_расход_водителя_в_смене_виден_по_машине_смены():
    """Водитель не выбирает машину у расхода — она берётся из его смены.
    Отбор по машине обязан это учитывать, иначе трата «пропадёт»."""
    async def body(s, c):
        s.add(Expense(owner_id=c["owner"].id, driver_id=c["driver"].id,
                      shift_id=c["shift"].id, category="parking",
                      amount_rub=Decimal("350"), status="pending"))
        await s.commit()
        flt = fl.ExpenseFilter(PERIOD.date_from, PERIOD.date_to, vehicle_id=c["truck"].id)
        rows = await fl.expense_rows(s, c["owner"].id, "Europe/Moscow", flt)
        assert len(rows) == 1 and rows[0].vehicle == "Т772НХ178"
        assert rows[0].driver == "Саломов" and rows[0].created_by == "driver"
        other = fl.ExpenseFilter(PERIOD.date_from, PERIOD.date_to, vehicle_id=c["other_truck"].id)
        assert await fl.expense_rows(s, c["owner"].id, "Europe/Moscow", other) == []
    _scenario(body)


def test_рейс_смена_и_книга_видят_одну_и_ту_же_запись():
    """Главное правило: деньги записываются один раз."""
    async def body(s, c):
        await fl.create_expenses(
            s, owner_id=c["owner"].id,
            items=[fl.NewExpense("toll", Decimal("1200"))],
            spent_at=None, vehicle_id=None, payment_method=None, supplier=None,
            trip_id=c["trip"].id,
        )
        await s.commit()
        всё = await fl.expense_rows(s, c["owner"].id, "Europe/Moscow", PERIOD)
        по_рейсу = await fl.expense_rows(s, c["owner"].id, "Europe/Moscow",
            fl.ExpenseFilter(PERIOD.date_from, PERIOD.date_to, trip_id=c["trip"].id))
        по_смене = await fl.expense_rows(s, c["owner"].id, "Europe/Moscow",
            fl.ExpenseFilter(PERIOD.date_from, PERIOD.date_to, shift_id=c["shift"].id))
        по_машине = await fl.expense_rows(s, c["owner"].id, "Europe/Moscow",
            fl.ExpenseFilter(PERIOD.date_from, PERIOD.date_to, vehicle_id=c["truck"].id))
        # Одна и та же строка во всех четырёх окнах, не копии.
        assert {r.id for r in всё} == {r.id for r in по_рейсу} == \
            {r.id for r in по_смене} == {r.id for r in по_машине}
        assert len(всё) == 1
    _scenario(body)


def test_ввод_с_ошибкой_объясняется_по_человечески():
    async def body(s, c):
        async def try_create(**kw):
            base = dict(owner_id=c["owner"].id, spent_at=None, vehicle_id=None,
                        payment_method=None, supplier=None)
            base.update(kw)
            with pytest.raises(fl.LedgerError) as err:
                await fl.create_expenses(s, **base)
            return str(err.value)

        assert "вид" in await try_create(items=[fl.NewExpense("выдумка", Decimal(5))])
        assert "больше нуля" in await try_create(items=[fl.NewExpense("fuel", Decimal(0))])
        assert "1 000 000" in await try_create(items=[fl.NewExpense("fuel", Decimal("2000000"))])
        assert "Прочего" in await try_create(items=[fl.NewExpense("other", Decimal(5))])
        # Чужую машину подставить нельзя.
        assert "машины" in await try_create(items=[fl.NewExpense("fuel", Decimal(5))],
                                            vehicle_id=c["foreign"].id)
        assert "способ" in await try_create(items=[fl.NewExpense("fuel", Decimal(5))],
                                            payment_method="биткоин")
        assert "хотя бы одну" in await try_create(items=[])
    _scenario(body)


def test_свой_вид_расхода_заводится_и_сразу_работает():
    async def body(s, c):
        code = await fl.add_category(s, c["owner"].id, "  Тахограф  ")
        assert code == "Тахограф"
        # Повтор имени — не ошибка и не дубль.
        assert await fl.add_category(s, c["owner"].id, "тахограф") == "Тахограф"
        # Совпало со встроенным — отдаём встроенный код.
        assert await fl.add_category(s, c["owner"].id, "мойка") == "wash"
        await fl.create_expenses(s, owner_id=c["owner"].id,
                                 items=[fl.NewExpense("Тахограф", Decimal("4500"))],
                                 spent_at=None, vehicle_id=None, payment_method=None, supplier=None)
        await s.commit()
        rows = await fl.expense_rows(s, c["owner"].id, "Europe/Moscow", PERIOD)
        assert rows[0].category_label == "Тахограф"
        # Свой вид виден только своему владельцу.
        assert "Тахограф" not in await fl.custom_categories(s, c["stranger"].id)
    _scenario(body)


def test_удаление_уносит_вложения_и_не_трогает_чужое():
    async def body(s, c):
        [expense] = await fl.create_expenses(
            s, owner_id=c["owner"].id,
            items=[fl.NewExpense("repair", Decimal("9000"), None, [
                fl.NewAttachment("photo", "a.jpg", "image/jpeg", b"x"),
                fl.NewAttachment("file", "b.pdf", "application/pdf", b"y"),
            ])],
            spent_at=None, vehicle_id=None, payment_method=None, supplier=None,
        )
        await s.commit()
        assert await fl.delete_expense(s, c["stranger"].id, expense.id) is False
        assert await fl.delete_expense(s, c["owner"].id, expense.id) is True
        await s.commit()
        left = await s.execute(select(func.count(ExpenseAttachment.id)))
        assert left.scalar_one() == 0
        assert await s.get(Expense, expense.id) is None
    _scenario(body)


def test_старые_ручные_расходы_не_пропадают_из_книги():
    async def body(s, c):
        s.add(ManualEntry(owner_id=c["owner"].id, type="expense", category="Аренда",
                          amount_rub=Decimal("30000"), entry_date=TODAY))
        await s.commit()
        rows = await fl.expense_rows(s, c["owner"].id, "Europe/Moscow", PERIOD)
        assert [r.source for r in rows] == ["manual"]
        assert rows[0].amount == Decimal("30000")
        # Отбор по машине ручную запись не показывает: машины у неё нет.
        flt = fl.ExpenseFilter(PERIOD.date_from, PERIOD.date_to, vehicle_id=c["truck"].id)
        assert await fl.expense_rows(s, c["owner"].id, "Europe/Moscow", flt) == []
    _scenario(body)


def test_доходы_это_рейсы_и_ручные_поступления():
    async def body(s, c):
        s.add(ManualEntry(owner_id=c["owner"].id, type="income", category="Возврат",
                          amount_rub=Decimal("5000"), entry_date=TODAY))
        await s.commit()
        rows = await fl.income_rows(s, c["owner"].id, "Europe/Moscow",
                                    PERIOD.date_from, PERIOD.date_to)
        assert sorted(r.amount for r in rows) == [Decimal("5000"), Decimal("18000")]
        рейс = next(r for r in rows if r.source == "trip")
        assert рейс.description == "Агропарк → Магнит" and рейс.vehicle == "Т772НХ178"
    _scenario(body)


def test_итоги_и_структура_считают_только_одобренное():
    async def body(s, c):
        s.add_all([
            Expense(owner_id=c["owner"].id, driver_id=c["driver"].id, category="fuel",
                    amount_rub=Decimal("1000"), status="approved"),
            Expense(owner_id=c["owner"].id, driver_id=c["driver"].id, category="fuel",
                    amount_rub=Decimal("500"), status="pending"),
            Expense(owner_id=c["owner"].id, driver_id=c["driver"].id, category="fine",
                    amount_rub=Decimal("250"), status="rejected"),
        ])
        await s.commit()
        rows = await fl.expense_rows(s, c["owner"].id, "Europe/Moscow", PERIOD)
        totals = fl.expense_totals(rows)
        assert totals["spent"] == Decimal("1000")
        assert totals["pending_count"] == 1 and totals["pending_sum"] == Decimal("500")
        # Без чека — одобренный и ждущий, а отклонённый уже не важен.
        assert totals["no_receipt_count"] == 2
        parts = fl.by_category(rows)
        assert [p["code"] for p in parts] == ["fuel"] and parts[0]["share"] == 100.0
    _scenario(body)


def test_сумма_принимает_человеческий_ввод():
    assert fl.parse_amount("3 500,50") == Decimal("3500.50")
    assert fl.parse_amount("3 500") == Decimal("3500.00")
    assert fl.parse_amount("0") is None
    assert fl.parse_amount("-5") is None
    assert fl.parse_amount("abc") is None
    assert fl.parse_amount("") is None


def test_денежный_поток_раскладывается_по_шагу_периода():
    inc = [fl.LedgerRow("trip", 1, datetime(2026, 9, 3, 9, tzinfo=timezone.utc),
                        Decimal("100"), "income")]
    exp = [fl.LedgerRow("expense", 2, datetime(2026, 9, 3, 10, tzinfo=timezone.utc),
                        Decimal("40"), "expense")]
    flow = fl.cashflow(inc, exp, date(2026, 9, 1), date(2026, 9, 7), "Europe/Moscow")
    assert flow["step"] == "day" and len(flow["labels"]) == 7
    assert flow["income"][2] == 100 and flow["expense"][2] == 40
    долгий = fl.cashflow([], [], date(2026, 1, 1), date(2026, 9, 30), "Europe/Moscow")
    assert долгий["step"] == "month" and len(долгий["labels"]) == 9
