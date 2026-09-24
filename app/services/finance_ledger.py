"""
Книга денег: расходы и доходы одним списком из всех источников.

Владелец 23.09.2026: «расходы же ещё должны быть в рейсах и сменах, поэтому
у нас тут всё запутано». Правило, которое эту путаницу снимает:

    Деньги записываются ОДИН раз. Рейс, смена и машина — это окна в ту же
    запись, а не свои копии.

Отсюда устройство:
* трата — одна строка в `expenses` (от водителя или от владельца);
* старые ручные записи владельца лежат в `manual_entries` — их не
  переносим, а читаем как есть, чтобы ничего не пропало;
* доход — выручка завершённых рейсов плюс ручные поступления.

Никакого деления на «основные» и «дополнительные» расходы, как у Завгара:
у нас список один, а привязка к машине — поле внутри записи (решение
владельца 22.09.2026).

⚠️ Топливо рейса (`trips.fuel_cost_rub`) сюда НЕ складывается: оно уже есть
среди расходов категории «Топливо». Сложить оба — двойной счёт.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Driver,
    Expense,
    ExpenseAttachment,
    ExpenseCategory,
    IncomeAttachment,
    ManualEntry,
    Shift,
    Trip,
    Vehicle,
)

# ── справочники ──────────────────────────────────────────────────────────────

# Встроенные виды расходов. Первые шесть были всегда (их шлёт бот водителя),
# остальные взяты из списка Завгара, который владелец показал 22.09.2026.
BUILTIN_CATEGORIES: dict[str, str] = {
    "fuel": "Топливо",
    "repair": "Ремонт",
    "parts": "Запчасти и расходники",
    "parking": "Парковка",
    "toll": "Платная дорога",
    "fine": "Штраф",
    "wash": "Мойка",
    "insurance": "Страховка",
    "permits": "Допуски и разрешения",
    "travel": "Командировочные",
    "other": "Прочее",
}

# Цвет вида на графиках. Топливо — всегда один и тот же синий: это главный
# расход, его глаз должен находить сразу на любой диаграмме.
CATEGORY_COLORS: dict[str, str] = {
    "fuel": "#0071e3",
    "repair": "#ff9f0a",
    "parts": "#bf5af2",
    "parking": "#30b0c7",
    "toll": "#5e5ce6",
    "fine": "#ff375f",
    "wash": "#64d2ff",
    "insurance": "#34c759",
    "permits": "#a2845e",
    "travel": "#ffd60a",
    "other": "#8e8e93",
}
CUSTOM_COLOR = "#636366"

PAYMENT_METHODS: dict[str, str] = {
    "cash": "Наличные",
    "card": "Карта",
    "fuel_card": "Топливная карта",
    "transfer": "Безнал",
    "advance": "Подотчёт",
}

# Верхний предел одной траты. Тот же, что у расхода водителя
# (expense_flow.MAX_AMOUNT): 1 000 000 ₽. Больше — почти наверняка опечатка.
MAX_AMOUNT = Decimal("1000000")
MAX_ITEMS = 20                     # трат в одном «документе»
MAX_ATTACHMENTS = 12               # вложений на одну трату
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic"}


def category_label(code: str | None) -> str:
    """Название вида. Свой вид хранится названием — его и показываем."""
    if not code:
        return "Прочее"
    return BUILTIN_CATEGORIES.get(code, code)


def category_color(code: str | None) -> str:
    return CATEGORY_COLORS.get(code or "", CUSTOM_COLOR)


def payment_label(code: str | None) -> str | None:
    return PAYMENT_METHODS.get(code or "") if code else None


def parse_amount(raw) -> Decimal | None:
    """«3 500,50» → Decimal('3500.50'). Мусор, ноль и минус — None."""
    if raw is None:
        return None
    text = str(raw).replace(" ", "").replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if value <= 0 or value.is_nan():
        return None
    return value.quantize(Decimal("0.01"))


# ── чтение ───────────────────────────────────────────────────────────────────

def expense_moment():
    """Когда потрачено: момент траты, а если его нет — момент внесения.

    ⚠️ По нему считают ВСЕ суммы расходов за период (главная, финансы,
    машина, итоги в Telegram). Владелец вносит трату задним числом — она
    обязана попасть в тот день, когда деньги ушли, а не когда их записали."""
    return func.coalesce(Expense.spent_at, Expense.created_at)


def _day(column, tz_name: str | None):
    """День в поясе владельца (как `_owner_day` в роутере — копия нарочно,
    чтобы служба не зависела от веб-слоя)."""
    return func.date(func.timezone(tz_name or "Europe/Moscow", column))


@dataclass
class LedgerRow:
    """Строка книги — одинаковая для расхода водителя, владельца и старой
    ручной записи. Шаблону не надо знать, откуда она."""
    source: str                    # expense · manual · trip
    id: int
    at: datetime | date | None
    amount: Decimal
    kind: str                      # expense · income
    category: str | None = None
    category_label: str = ""
    color: str = CUSTOM_COLOR
    vehicle_id: int | None = None
    vehicle: str | None = None
    driver: str | None = None
    status: str = "approved"
    payment: str | None = None
    supplier: str | None = None
    description: str | None = None
    trip_id: int | None = None
    shift_id: int | None = None
    created_by: str | None = None
    has_receipt: bool = False
    photos: int = 0
    files: int = 0
    attachments: list[dict] = field(default_factory=list)
    # Чек старого образца: фото водителя из бота/приложения (ключ снимка) или
    # файл, загруженный владельцем на странице правки. Во вложения не переносим.
    legacy_photo: str | None = None
    legacy_web: bool = False


@dataclass
class ExpenseFilter:
    date_from: date
    date_to: date
    category: str | None = None
    vehicle_id: int | None = None
    payment: str | None = None
    receipt: str | None = None       # with · without
    status: str | None = None        # approved · pending · rejected
    trip_id: int | None = None
    shift_id: int | None = None
    driver_id: int | None = None


async def expense_rows(
    session: AsyncSession, owner_id: int, tz_name: str | None, flt: ExpenseFilter,
) -> list[LedgerRow]:
    """Все расходы за период: из `expenses` и старые ручные из `manual_entries`."""
    vehicle_of = func.coalesce(Expense.vehicle_id, Shift.vehicle_id, Trip.vehicle_id)
    conditions = [
        Expense.owner_id == owner_id,
        _day(expense_moment(), tz_name) >= flt.date_from,
        _day(expense_moment(), tz_name) <= flt.date_to,
    ]
    if flt.category:
        conditions.append(Expense.category == flt.category)
    if flt.vehicle_id:
        conditions.append(vehicle_of == flt.vehicle_id)
    if flt.payment:
        conditions.append(Expense.payment_method == flt.payment)
    if flt.status:
        conditions.append(Expense.status == flt.status)
    if flt.trip_id:
        conditions.append(Expense.trip_id == flt.trip_id)
    if flt.shift_id:
        conditions.append(Expense.shift_id == flt.shift_id)
    if flt.driver_id:
        conditions.append(Expense.driver_id == flt.driver_id)

    result = await session.execute(
        select(Expense, Driver.full_name, Vehicle.id, Vehicle.license_plate)
        # ⚠️ Внешние соединения везде: у расхода владельца нет ни водителя,
        # ни смены, ни рейса. Внутреннее соединение молча выбросило бы его.
        .outerjoin(Driver, Driver.id == Expense.driver_id)
        .outerjoin(Shift, Shift.id == Expense.shift_id)
        .outerjoin(Trip, Trip.id == Expense.trip_id)
        .outerjoin(Vehicle, Vehicle.id == vehicle_of)
        .where(and_(*conditions))
        .order_by(expense_moment().desc(), Expense.id.desc())
    )
    found = result.all()
    ids = [e.id for e, *_ in found]

    attachments: dict[int, list[dict]] = {}
    if ids:
        att_res = await session.execute(
            select(
                ExpenseAttachment.id, ExpenseAttachment.expense_id,
                ExpenseAttachment.kind, ExpenseAttachment.filename,
                ExpenseAttachment.content_type, ExpenseAttachment.size_bytes,
            )
            .where(ExpenseAttachment.expense_id.in_(ids))
            .order_by(ExpenseAttachment.id)
        )
        for att_id, exp_id, kind, name, ctype, size in att_res.all():
            attachments.setdefault(exp_id, []).append({
                "id": att_id, "kind": kind, "filename": name,
                "content_type": ctype, "size": size,
            })

    rows: list[LedgerRow] = []
    for expense, driver_name, vid, plate in found:
        atts = attachments.get(expense.id, [])
        # Чек водителя (фото из бота или приложения) и чек, загруженный
        # владельцем раньше, — тоже подтверждение, хоть и не во вложениях.
        legacy_receipt = bool(expense.receipt_photo_url or expense.receipt_web_data)
        photos = sum(1 for a in atts if a["kind"] == "photo") + (1 if legacy_receipt else 0)
        files = sum(1 for a in atts if a["kind"] == "file")
        rows.append(LedgerRow(
            source="expense", id=expense.id,
            at=expense.spent_at or expense.created_at,
            amount=Decimal(expense.amount_rub or 0), kind="expense",
            category=expense.category, category_label=category_label(expense.category),
            color=category_color(expense.category),
            vehicle_id=vid, vehicle=plate, driver=driver_name,
            status=expense.status or "approved",
            payment=payment_label(expense.payment_method),
            supplier=expense.supplier, description=expense.description,
            trip_id=expense.trip_id, shift_id=expense.shift_id,
            created_by=expense.created_by or ("driver" if expense.driver_id else "owner"),
            has_receipt=bool(photos or files), photos=photos, files=files,
            attachments=atts,
            legacy_photo=expense.receipt_photo_url,
            legacy_web=bool(expense.receipt_web_data),
        ))

    # Старые ручные расходы владельца. У них нет машины, чека и способа
    # оплаты — поэтому при отборе по ним они не подходят и не показываются.
    manual_fits = not (flt.vehicle_id or flt.payment or flt.trip_id or flt.shift_id
                       or flt.driver_id or flt.receipt == "with" or flt.status in ("pending", "rejected"))
    if manual_fits:
        m_conditions = [
            ManualEntry.owner_id == owner_id,
            ManualEntry.type == "expense",
            ManualEntry.entry_date >= flt.date_from,
            ManualEntry.entry_date <= flt.date_to,
        ]
        if flt.category:
            m_conditions.append(ManualEntry.category == flt.category)
        m_res = await session.execute(
            select(ManualEntry).where(and_(*m_conditions))
            .order_by(ManualEntry.entry_date.desc(), ManualEntry.id.desc())
        )
        for entry in m_res.scalars().all():
            rows.append(LedgerRow(
                source="manual", id=entry.id, at=entry.entry_date,
                amount=Decimal(entry.amount_rub or 0), kind="expense",
                category=entry.category, category_label=category_label(entry.category),
                color=category_color(entry.category),
                description=entry.description, created_by="owner",
            ))

    if flt.receipt == "without":
        rows = [r for r in rows if not r.has_receipt]
    elif flt.receipt == "with":
        rows = [r for r in rows if r.has_receipt]

    rows.sort(key=_sort_key, reverse=True)
    return rows


def _sort_key(row: LedgerRow):
    """Сортировка смеси «дата» и «дата со временем» по одной оси."""
    at = row.at
    if at is None:
        return (datetime.min.replace(tzinfo=timezone.utc), row.id)
    if isinstance(at, datetime):
        moment = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    else:
        # Ручная запись без времени — считаем полднем того дня, чтобы она не
        # прыгала в самый низ дня относительно трат с точным временем.
        moment = datetime(at.year, at.month, at.day, 12, tzinfo=timezone.utc)
    return (moment, row.id)


def expense_totals(rows: list[LedgerRow]) -> dict:
    """Три числа над книгой: потрачено · ждёт решения · без подтверждения."""
    spent = sum((r.amount for r in rows if r.status == "approved"), Decimal(0))
    pending = [r for r in rows if r.status == "pending"]
    no_receipt = [r for r in rows if r.source == "expense" and not r.has_receipt
                  and r.status != "rejected"]
    return {
        "spent": spent,
        "count": sum(1 for r in rows if r.status == "approved"),
        "pending_sum": sum((r.amount for r in pending), Decimal(0)),
        "pending_count": len(pending),
        "no_receipt_count": len(no_receipt),
        "no_receipt_sum": sum((r.amount for r in no_receipt), Decimal(0)),
    }


def by_category(rows: list[LedgerRow]) -> list[dict]:
    """Структура расходов для кольца: только одобренные, по убыванию суммы."""
    buckets: dict[str, Decimal] = {}
    for r in rows:
        if r.status != "approved":
            continue
        key = r.category or "other"
        buckets[key] = buckets.get(key, Decimal(0)) + r.amount
    total = sum(buckets.values(), Decimal(0))
    out = [
        {
            "code": code, "label": category_label(code), "color": category_color(code),
            "amount": float(amount),
            "share": float(amount / total * 100) if total else 0.0,
        }
        for code, amount in buckets.items()
    ]
    out.sort(key=lambda x: x["amount"], reverse=True)
    return out


def by_vehicle(rows: list[LedgerRow]) -> list[dict]:
    """Расходы по машинам. Всё без машины — одной строкой «Компания»."""
    buckets: dict[str, Decimal] = {}
    for r in rows:
        if r.status != "approved":
            continue
        key = r.vehicle or "Компания"
        buckets[key] = buckets.get(key, Decimal(0)) + r.amount
    out = [{"label": k, "amount": float(v)} for k, v in buckets.items()]
    out.sort(key=lambda x: x["amount"], reverse=True)
    return out


async def income_rows(
    session: AsyncSession, owner_id: int, tz_name: str | None,
    date_from: date, date_to: date, vehicle_id: int | None = None,
) -> list[LedgerRow]:
    """Доходы: выручка завершённых рейсов и ручные поступления."""
    conditions = [
        Trip.owner_id == owner_id,
        Trip.status == "completed",
        Trip.revenue_rub.is_not(None),
        _day(Trip.completed_at, tz_name) >= date_from,
        _day(Trip.completed_at, tz_name) <= date_to,
    ]
    if vehicle_id:
        conditions.append(Trip.vehicle_id == vehicle_id)
    trips = await session.execute(
        select(Trip, Driver.full_name, Vehicle.license_plate)
        .outerjoin(Driver, Driver.id == Trip.driver_id)
        .outerjoin(Vehicle, Vehicle.id == Trip.vehicle_id)
        .where(and_(*conditions))
        .order_by(Trip.completed_at.desc())
    )
    rows = [
        LedgerRow(
            source="trip", id=trip.id, at=trip.completed_at,
            amount=Decimal(trip.revenue_rub or 0), kind="income",
            category="trip", category_label="Рейс",
            color="#34c759", vehicle_id=trip.vehicle_id, vehicle=plate,
            driver=driver_name,
            description=f"{trip.origin or '—'} → {trip.destination or '—'}",
            trip_id=trip.id, shift_id=trip.shift_id,
        )
        for trip, driver_name, plate in trips.all()
    ]
    if not vehicle_id:
        manual = await session.execute(
            select(ManualEntry).where(
                ManualEntry.owner_id == owner_id,
                ManualEntry.type == "income",
                ManualEntry.entry_date >= date_from,
                ManualEntry.entry_date <= date_to,
            ).order_by(ManualEntry.entry_date.desc(), ManualEntry.id.desc())
        )
        entries = manual.scalars().all()
        atts = await _income_attachments(session, [e.id for e in entries])
        for entry in entries:
            own = atts.get(entry.id, [])
            photos = sum(1 for a in own if a["kind"] == "photo")
            rows.append(LedgerRow(
                source="manual", id=entry.id, at=entry.entry_date,
                amount=Decimal(entry.amount_rub or 0), kind="income",
                category=entry.category, category_label=entry.category or "Поступление",
                color="#30d158", description=entry.description, created_by="owner",
                attachments=own, photos=photos, files=len(own) - photos,
                has_receipt=bool(own),
            ))
    rows.sort(key=_sort_key, reverse=True)
    return rows


async def _income_attachments(session: AsyncSession, entry_ids: list[int]) -> dict[int, list[dict]]:
    """Вложения поступлений — одним запросом, без самих байтов."""
    if not entry_ids:
        return {}
    res = await session.execute(
        select(
            IncomeAttachment.id, IncomeAttachment.manual_entry_id, IncomeAttachment.kind,
            IncomeAttachment.filename, IncomeAttachment.content_type, IncomeAttachment.size_bytes,
        )
        .where(IncomeAttachment.manual_entry_id.in_(entry_ids))
        .order_by(IncomeAttachment.id)
    )
    out: dict[int, list[dict]] = {}
    for att_id, entry_id, kind, name, ctype, size in res.all():
        out.setdefault(entry_id, []).append({
            "id": att_id, "kind": kind, "filename": name, "content_type": ctype, "size": size,
        })
    return out


def cashflow(incomes: list[LedgerRow], expenses: list[LedgerRow],
             date_from: date, date_to: date, tz_name: str | None) -> dict:
    """Доходы и расходы по дням, неделям или месяцам — смотря по длине периода."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz_name or "Europe/Moscow")
    days = (date_to - date_from).days + 1
    step = "day" if days <= 31 else ("week" if days <= 120 else "month")

    def bucket_of(at) -> date | None:
        if at is None:
            return None
        if isinstance(at, datetime):
            local = (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(zone).date()
        else:
            local = at
        if step == "day":
            return local
        if step == "week":
            return date.fromordinal(local.toordinal() - local.weekday())
        return local.replace(day=1)

    keys: list[date] = []
    cursor = bucket_of(date_from)
    last = bucket_of(date_to)
    while cursor and last and cursor <= last and len(keys) < 400:
        keys.append(cursor)
        if step == "day":
            cursor = date.fromordinal(cursor.toordinal() + 1)
        elif step == "week":
            cursor = date.fromordinal(cursor.toordinal() + 7)
        else:
            cursor = (cursor.replace(day=28).toordinal() + 4)
            cursor = date.fromordinal(cursor).replace(day=1)

    inc = {k: Decimal(0) for k in keys}
    exp = {k: Decimal(0) for k in keys}
    fuel = {k: Decimal(0) for k in keys}
    for r in incomes:
        k = bucket_of(r.at)
        if k in inc:
            inc[k] += r.amount
    for r in expenses:
        if r.status != "approved":
            continue
        k = bucket_of(r.at)
        if k in exp:
            exp[k] += r.amount
            if r.category == "fuel":
                fuel[k] += r.amount

    months = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

    def label(k: date) -> str:
        if step == "month":
            return months[k.month - 1]
        return f"{k.day} {months[k.month - 1]}"

    return {
        "step": step,
        "labels": [label(k) for k in keys],
        "income": [float(inc[k]) for k in keys],
        "expense": [float(exp[k]) for k in keys],
        # Топливо — отдельной линией поверх расходов: владелец 23.09 спросил,
        # как отделить топливо от прочего. Отдельный список денег не нужен —
        # нужен отдельный взгляд на ту же книгу.
        "fuel": [float(fuel[k]) for k in keys],
    }


async def custom_categories(session: AsyncSession, owner_id: int) -> list[str]:
    result = await session.execute(
        select(ExpenseCategory.name)
        .where(ExpenseCategory.owner_id == owner_id)
        .order_by(ExpenseCategory.name)
    )
    return [name for (name,) in result.all()]


async def all_categories(session: AsyncSession, owner_id: int) -> list[dict]:
    """Встроенные виды и свои — для выпадающего списка."""
    items = [{"code": code, "label": label, "custom": False}
             for code, label in BUILTIN_CATEGORIES.items()]
    for name in await custom_categories(session, owner_id):
        items.append({"code": name, "label": name, "custom": True})
    return items


async def add_category(session: AsyncSession, owner_id: int, name: str) -> str:
    """Завести свой вид. Повтор имени — не ошибка, отдаём существующий."""
    clean = " ".join((name or "").split())[:60]
    if not clean:
        raise ValueError("Название пустое.")
    key = clean.casefold()
    for code, label in BUILTIN_CATEGORIES.items():
        if label.casefold() == key:
            return code
    # ⚠️ Сравниваем в Python, а не через SQL lower(): SQLite переводит в
    # нижний регистр только латиницу, и «Тахограф» с «тахограф» считались бы
    # разными видами. casefold() одинаково работает на любой базе.
    for name in await custom_categories(session, owner_id):
        if name.casefold() == key:
            return name
    session.add(ExpenseCategory(owner_id=owner_id, name=clean))
    await session.flush()
    return clean


# ── запись ───────────────────────────────────────────────────────────────────

class LedgerError(ValueError):
    """Ошибка ввода — текст готов для человека."""


@dataclass
class NewAttachment:
    kind: str
    filename: str | None
    content_type: str | None
    data: bytes


@dataclass
class NewExpense:
    category: str
    amount: Decimal
    description: str | None = None
    attachments: list[NewAttachment] = field(default_factory=list)


async def create_expenses(
    session: AsyncSession,
    *,
    owner_id: int,
    items: list[NewExpense],
    spent_at: datetime | None,
    vehicle_id: int | None,
    payment_method: str | None,
    supplier: str | None,
    trip_id: int | None = None,
    shift_id: int | None = None,
) -> list[Expense]:
    """Внести несколько трат одним документом (коммит — на вызывающем).

    Общее для всех трат: дата, машина, способ оплаты, поставщик, рейс,
    смена. У каждой своё: вид, сумма, комментарий, вложения.
    Расход, внесённый владельцем, сразу одобрен: сам себе он не отказывает.
    """
    if not items:
        raise LedgerError("Добавьте хотя бы одну трату.")
    if len(items) > MAX_ITEMS:
        raise LedgerError(f"Не больше {MAX_ITEMS} трат за один раз.")
    if payment_method and payment_method not in PAYMENT_METHODS:
        raise LedgerError("Неизвестный способ оплаты.")

    known = set(BUILTIN_CATEGORIES) | set(await custom_categories(session, owner_id))
    for number, item in enumerate(items, 1):
        if item.category not in known:
            raise LedgerError(f"Трата №{number}: выберите вид расхода.")
        if item.amount is None or item.amount <= 0:
            raise LedgerError(f"Трата №{number}: укажите сумму больше нуля.")
        if item.amount > MAX_AMOUNT:
            raise LedgerError(f"Трата №{number}: не больше 1 000 000 ₽ за раз.")
        if item.category == "other" and not (item.description or "").strip():
            raise LedgerError(f"Трата №{number}: для «Прочего» коротко напишите, что это.")
        if len(item.attachments) > MAX_ATTACHMENTS:
            raise LedgerError(f"Трата №{number}: не больше {MAX_ATTACHMENTS} вложений.")
        for att in item.attachments:
            if len(att.data) > MAX_ATTACHMENT_BYTES:
                raise LedgerError(f"Трата №{number}: файл «{att.filename}» больше 15 МБ.")

    # Машина, рейс и смена должны быть свои — чужие номера не принимаем.
    if vehicle_id is not None:
        owned = await session.execute(
            select(Vehicle.id).where(Vehicle.id == vehicle_id, Vehicle.owner_id == owner_id)
        )
        if owned.scalar_one_or_none() is None:
            raise LedgerError("Такой машины нет в парке.")
    trip = None
    if trip_id is not None:
        trip = await session.get(Trip, trip_id)
        if trip is None or trip.owner_id != owner_id:
            raise LedgerError("Такого рейса нет.")
    if shift_id is not None:
        shift = await session.get(Shift, shift_id)
        if shift is None or shift.owner_id != owner_id:
            raise LedgerError("Такой смены нет.")
    # Расход к рейсу — сразу и к его смене и машине, чтобы окна рейса,
    # смены и машины показывали одну и ту же запись.
    if trip is not None:
        shift_id = shift_id or trip.shift_id
        vehicle_id = vehicle_id or trip.vehicle_id

    batch = uuid.uuid4().hex if len(items) > 1 else None
    created: list[Expense] = []
    for item in items:
        expense = Expense(
            owner_id=owner_id, driver_id=None, vehicle_id=vehicle_id,
            trip_id=trip_id, shift_id=shift_id,
            category=item.category, amount_rub=item.amount,
            description=(item.description or "").strip() or None,
            payment_method=payment_method or None,
            supplier=(supplier or "").strip()[:255] or None,
            spent_at=spent_at, created_by="owner", batch_id=batch,
            status="approved", decided_at=datetime.now(timezone.utc),
        )
        session.add(expense)
        await session.flush()
        for att in item.attachments:
            session.add(ExpenseAttachment(
                owner_id=owner_id, expense_id=expense.id,
                kind="photo" if att.kind == "photo" else "file",
                filename=(att.filename or "")[:255] or None,
                content_type=(att.content_type or "")[:100] or None,
                size_bytes=len(att.data), data=att.data,
            ))
        created.append(expense)
    await session.flush()
    # Трата к рейсу меняет его прибыль — пересчитываем сразу (одна книга).
    from app.services import trip_service
    await trip_service.refresh_trip_costs(session, trip_id)
    return created


async def delete_expense(session: AsyncSession, owner_id: int, expense_id: int) -> bool:
    """Удалить расход вместе с вложениями. Чужой или несуществующий — False."""
    expense = await session.get(Expense, expense_id)
    if expense is None or expense.owner_id != owner_id:
        return False
    # ⚠️ Вложения удаляем явно: каскад в базе есть, но SQLite в тестах держит
    # внешние ключи выключенными, и там вложения остались бы сиротами.
    await session.execute(
        delete(ExpenseAttachment).where(ExpenseAttachment.expense_id == expense.id)
    )
    trip_id = expense.trip_id
    await session.delete(expense)
    await session.flush()
    from app.services import trip_service
    await trip_service.refresh_trip_costs(session, trip_id)
    return True


MAX_INCOME = MAX_AMOUNT * 10     # поступление за раз: до 10 млн ₽ (оплата по акту за месяц)


async def create_income(
    session: AsyncSession, *, owner_id: int, amount: Decimal | None, day: date,
    category: str | None, description: str | None, attachments: list[NewAttachment],
) -> ManualEntry:
    """Ручное поступление с вложениями (платёжка, акт, выписка). Коммит — на
    вызывающем. Выручка рейсов сюда НЕ пишется — она приходит из рейса сама."""
    if amount is None or amount <= 0:
        raise LedgerError("Укажите сумму больше нуля.")
    if amount > MAX_INCOME:
        raise LedgerError("Не больше 10 000 000 ₽ за раз.")
    if len(attachments) > MAX_ATTACHMENTS:
        raise LedgerError(f"Не больше {MAX_ATTACHMENTS} вложений.")
    for att in attachments:
        if len(att.data) > MAX_ATTACHMENT_BYTES:
            raise LedgerError(f"Файл «{att.filename}» больше 15 МБ.")
    entry = ManualEntry(
        owner_id=owner_id, type="income", amount_rub=amount, entry_date=day,
        category=(category or "").strip()[:100] or None,
        description=(description or "").strip()[:500] or None,
    )
    session.add(entry)
    await session.flush()
    for att in attachments:
        session.add(IncomeAttachment(
            owner_id=owner_id, manual_entry_id=entry.id,
            kind="photo" if att.kind == "photo" else "file",
            filename=(att.filename or "")[:255] or None,
            content_type=(att.content_type or "")[:100] or None,
            size_bytes=len(att.data), data=att.data,
        ))
    await session.flush()
    return entry


async def delete_manual_entry(session: AsyncSession, owner_id: int, entry_id: int) -> bool:
    """Удалить ручную запись вместе с вложениями. Чужая или нет — False."""
    entry = await session.get(ManualEntry, entry_id)
    if entry is None or entry.owner_id != owner_id:
        return False
    # ⚠️ Явно, как у расходов: на базе без каскадов вложения остались бы сиротами.
    await session.execute(
        delete(IncomeAttachment).where(IncomeAttachment.manual_entry_id == entry.id)
    )
    await session.delete(entry)
    await session.flush()
    return True


def kind_of_upload(content_type: str | None, filename: str | None) -> str:
    """Фото или файл — по типу, а если его нет, по расширению."""
    ctype = (content_type or "").lower()
    if ctype in PHOTO_TYPES:
        return "photo"
    name = (filename or "").lower()
    if name.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic")):
        return "photo"
    return "file"
