"""
Раздел «Финансы»: Обзор · Расходы · Доходы.

Владелец 23.09.2026: «начинаем с финансов; стрелочка, которая открывается,
а там расходы, доходы; много графиков; всё, что было у Завгара по
расходам; и расходы должны быть в рейсах и сменах».

Устройство:
* **Обзор** — графики и итоги, ничего не вводится;
* **Расходы** — одна книга трат (водителя и владельца), отборы, ввод
  нескольких трат одним документом, фото и файлы, решения по тратам
  водителя;
* **Доходы** — выручка рейсов и ручные поступления.

Данные собирает `app/services/finance_ledger.py`. Здесь — только веб.

⚠️ Модуль подключается в САМОМ КОНЦЕ `router.py` (`from app.web import
finance_routes`): ему нужны `app`, `templates` и зависимости оттуда, а
регистрация маршрутов позже прочих ничему не мешает — старый `/finances`
из роутера удалён, пересечений нет.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Driver, Expense, ExpenseAttachment, IncomeAttachment, Owner, Shift, Trip, Vehicle,
)
from app.services import driver_photos
from app.services import finance_ledger as fl
from app.web.router import (
    _owner_day,
    _parse_period,
    app,
    current_owner,
    get_session,
    templates,
)

_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]
_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _rub(value) -> str:
    """12400.5 → «12 400,50 ₽»; целые — без копеек; минус — настоящий «−».
    Узкий пробел между разрядами не даёт числу разорваться на две строки."""
    amount = Decimal(str(value or 0))
    sign = "−" if amount < 0 else ""
    amount = abs(amount)
    whole = amount == amount.to_integral_value()
    text = f"{amount:,.0f}" if whole else f"{amount:,.2f}"
    return sign + text.replace(",", "\u202f").replace(".", ",") + "\u00a0₽"


def _plural(n: int, one: str, few: str, many: str) -> str:
    """1 трата, 2 траты, 5 трат."""
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _script_json(data) -> str:
    """JSON для <script type="application/json">: «</» внутри названий
    (маршрут, номер машины) не должно закрыть тег раньше времени."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


templates.env.filters["fin_rub"] = _rub
templates.env.globals["fin_plural"] = _plural


def _zone(owner: Owner) -> ZoneInfo:
    try:
        return ZoneInfo(owner.timezone or "Europe/Moscow")
    except Exception:                                    # noqa: BLE001
        return ZoneInfo("Europe/Moscow")


def _local_date(at, zone: ZoneInfo) -> date | None:
    if at is None:
        return None
    if isinstance(at, datetime):
        return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(zone).date()
    return at


def _day_title(day: date, today: date) -> str:
    """«Сегодня», «Вчера», «21 сентября, пн» — как в ленте у Apple."""
    if day == today:
        return "Сегодня"
    if day == today - timedelta(days=1):
        return "Вчера"
    title = f"{day.day} {_MONTHS[day.month - 1]}"
    if day.year != today.year:
        title += f" {day.year}"
    return f"{title}, {_WEEKDAYS[day.weekday()]}"


def _group_by_day(rows: list[fl.LedgerRow], zone: ZoneInfo) -> list[dict]:
    today = datetime.now(zone).date()
    groups: list[dict] = []
    for row in rows:
        day = _local_date(row.at, zone)
        if not groups or groups[-1]["day"] != day:
            groups.append({"day": day, "title": _day_title(day, today) if day else "Без даты",
                           "rows": [], "total": Decimal(0)})
        groups[-1]["rows"].append(row)
        if row.status == "approved":
            groups[-1]["total"] += row.amount
    return groups


def _time_of(row: fl.LedgerRow, zone: ZoneInfo) -> str | None:
    if isinstance(row.at, datetime):
        at = row.at if row.at.tzinfo else row.at.replace(tzinfo=timezone.utc)
        return at.astimezone(zone).strftime("%H:%M")
    return None


templates.env.globals["fin_time"] = _time_of


def _when(at, zone: ZoneInfo) -> str:
    """«23 сентября 2026, 14:05» — для раскрытой строки."""
    if at is None:
        return "—"
    if isinstance(at, datetime):
        local = (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(zone)
        return f"{local.day} {_MONTHS[local.month - 1]} {local.year}, {local:%H:%M}"
    return f"{at.day} {_MONTHS[at.month - 1]} {at.year}"


templates.env.globals["fin_when"] = _when

# Значок вида расхода (Phosphor). Свой вид — ярлычок.
_ICONS = {
    "fuel": "gas-pump", "repair": "wrench", "parts": "gear-six", "parking": "garage",
    "toll": "road-horizon", "fine": "receipt-x", "wash": "drop", "insurance": "shield-check",
    "permits": "certificate", "travel": "suitcase-rolling", "other": "dots-three-circle",
    "trip": "truck", "income": "hand-coins",
}
templates.env.globals["fin_icon"] = lambda code: _ICONS.get(code or "", "tag")
templates.env.globals["fin_cat"] = fl.category_label


def _fin_url(request: Request, **changes) -> str:
    """Тот же адрес с заменёнными отборами. None — убрать отбор."""
    params = dict(request.query_params)
    for key in ("added", "new"):
        params.pop(key, None)
    for key, value in changes.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = str(value)
    from urllib.parse import urlencode
    query = urlencode(params)
    return request.url.path + (f"?{query}" if query else "")


templates.env.globals["fin_url"] = _fin_url


async def _vehicles(session: AsyncSession, owner_id: int) -> list[Vehicle]:
    result = await session.execute(
        select(Vehicle).where(Vehicle.owner_id == owner_id, Vehicle.is_active.is_(True))
        .order_by(Vehicle.license_plate)
    )
    return list(result.scalars().all())


def _period_context(df: date, dt: date) -> dict:
    """Быстрые периоды — как «Сегодня / Неделя / Месяц» в Apple Здоровье."""
    today = date.today()
    presets = [
        ("month", "Этот месяц", today.replace(day=1), today),
        ("7", "7 дней", today - timedelta(days=6), today),
        ("30", "30 дней", today - timedelta(days=29), today),
        ("91", "Квартал", today - timedelta(days=90), today),
        ("365", "Год", today - timedelta(days=364), today),
    ]
    active = next((key for key, _, a, b in presets if a == df and b == dt), "custom")
    if df <= _ALL_TIME_FROM:
        active = "all"
    return {
        "period_from": df.isoformat(), "period_to": dt.isoformat(),
        "period_presets": [
            {"key": key, "label": label, "from": a.isoformat(), "to": b.isoformat()}
            for key, label, a, b in presets
        ],
        "period_active": active,
        "period_label": "За всё время" if active == "all" else
        f"{df.day} {_MONTHS[df.month - 1]} — {dt.day} {_MONTHS[dt.month - 1]} {dt.year}",
    }


# Рейс и смена открывают свои траты за всё время: рейс прошлого месяца не
# должен выглядеть пустым из-за того, что по умолчанию стоит «этот месяц».
_ALL_TIME_FROM = date(2000, 1, 1)


async def _nav_counts(session: AsyncSession, owner_id: int) -> dict:
    """Числа у пунктов меню: сколько трат ждёт решения владельца."""
    pending = await session.execute(
        select(func.count(Expense.id)).where(
            Expense.owner_id == owner_id, Expense.status == "pending",
        )
    )
    return {"pending": pending.scalar_one() or 0}


# ── Обзор ────────────────────────────────────────────────────────────────────

@app.get("/finances", response_class=HTMLResponse)
async def finance_overview(
    request: Request,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    period_from: Annotated[str | None, Query()] = None,
    period_to: Annotated[str | None, Query()] = None,
):
    df, dt = _parse_period(period_from, period_to)
    tz = owner.timezone
    incomes = await fl.income_rows(session, owner.id, tz, df, dt)
    expenses = await fl.expense_rows(session, owner.id, tz, fl.ExpenseFilter(df, dt))

    income_total = sum((r.amount for r in incomes), Decimal(0))
    expense_total = sum((r.amount for r in expenses if r.status == "approved"), Decimal(0))
    profit = income_total - expense_total
    margin = float(profit / income_total * 100) if income_total > 0 else None

    # Прибыль по машинам: доход рейсов машины минус её одобренные траты.
    per_vehicle: dict[str, dict] = {}
    # «Компания» — всё, что не на машине: и доход, и расход (одна строка,
    # а не «Без машины» и «За компанию» про одно и то же).
    for r in incomes:
        key = r.vehicle or "Компания"
        per_vehicle.setdefault(key, {"label": key, "income": 0.0, "expense": 0.0})
        per_vehicle[key]["income"] += float(r.amount)
    for r in expenses:
        if r.status != "approved":
            continue
        key = r.vehicle or "Компания"
        per_vehicle.setdefault(key, {"label": key, "income": 0.0, "expense": 0.0})
        per_vehicle[key]["expense"] += float(r.amount)
    vehicles = sorted(per_vehicle.values(), key=lambda v: v["income"] - v["expense"], reverse=True)
    top = max([max(v["income"], v["expense"]) for v in vehicles] or [0]) or 1
    for v in vehicles:
        v["profit"] = v["income"] - v["expense"]
        v["w_income"] = round(v["income"] / top * 100, 1)
        v["w_expense"] = round(v["expense"] / top * 100, 1)

    # Прибыльность направлений — как было на старой странице финансов.
    # Направления: выручка, средний рейс и траты, привязанные к рейсам.
    # ⚠️ Не «прибыль рейса» (trips.profit_rub): траты из книги она не видит,
    # и у рейса без внесённых затрат «прибыль» совпадала бы с выручкой.
    trip_res = await session.execute(
        select(Trip.id, Trip.origin, Trip.destination, Trip.revenue_rub).where(
            Trip.owner_id == owner.id, Trip.status == "completed",
            _owner_day(Trip.completed_at, tz) >= df, _owner_day(Trip.completed_at, tz) <= dt,
        )
    )
    trip_costs: dict[int, Decimal] = {}
    for r in expenses:
        if r.status == "approved" and r.trip_id:
            trip_costs[r.trip_id] = trip_costs.get(r.trip_id, Decimal(0)) + r.amount
    by_route: dict[str, dict] = {}
    for tid, origin, destination, revenue in trip_res.all():
        key = f"{origin or '—'} → {destination or '—'}"
        d = by_route.setdefault(key, {"route": key, "trips": 0, "revenue": 0.0, "costs": 0.0})
        d["trips"] += 1
        d["revenue"] += float(revenue or 0)
        d["costs"] += float(trip_costs.get(tid, 0))
    directions = sorted(by_route.values(), key=lambda d: d["revenue"], reverse=True)[:6]
    for d in directions:
        d["avg"] = d["revenue"] / d["trips"] if d["trips"] else 0

    # Топливо — главный расход, у него своя карточка. Отдельного списка
    # денег под топливо нет: это взгляд на ту же книгу (решение 23.09.2026).
    top_rev = max([d["revenue"] for d in directions] or [0]) or 1
    for d in directions:
        d["w"] = round(d["revenue"] / top_rev * 100, 1)

    fuel_total = sum((r.amount for r in expenses
                      if r.status == "approved" and r.category == "fuel"), Decimal(0))
    fuel_share = float(fuel_total / expense_total * 100) if expense_total > 0 else None

    zone = _zone(owner)
    charts = {
        "cashflow": fl.cashflow(incomes, expenses, df, dt, tz),
        "categories": fl.by_category(expenses),
    }
    return templates.TemplateResponse("finances.html", {
        "request": request, "owner": owner, "active_page": "finances",
        "fin_tab": "overview", "counts": await _nav_counts(session, owner.id),
        "income_total": income_total, "expense_total": expense_total,
        "profit": profit, "margin": margin,
        "fuel_total": fuel_total, "fuel_share": fuel_share,
        "other_total": expense_total - fuel_total,
        "pending": fl.expense_totals(expenses),
        "zone": zone,
        "charts_json": _script_json(charts),
        "vehicles": vehicles, "directions": directions,
        "cashflow_step": charts["cashflow"]["step"],
        "has_data": bool(incomes or expenses),
        **_period_context(df, dt),
    })


# ── Расходы ──────────────────────────────────────────────────────────────────

@app.get("/finances/expenses", response_class=HTMLResponse)
async def finance_expenses(
    request: Request,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    period_from: Annotated[str | None, Query()] = None,
    period_to: Annotated[str | None, Query()] = None,
    cat: Annotated[str | None, Query()] = None,
    vehicle: Annotated[int | None, Query()] = None,
    pay: Annotated[str | None, Query()] = None,
    receipt: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    trip: Annotated[int | None, Query()] = None,
    shift: Annotated[int | None, Query()] = None,
    driver: Annotated[int | None, Query()] = None,
    new: Annotated[int | None, Query()] = None,
    added: Annotated[str | None, Query()] = None,
):
    df, dt = _parse_period(period_from, period_to)
    if (trip or shift) and not period_from:
        df, dt = _ALL_TIME_FROM, date.today() + timedelta(days=1)
    flt = fl.ExpenseFilter(
        date_from=df, date_to=dt,
        category=cat or None, vehicle_id=vehicle or None,
        payment=pay if pay in fl.PAYMENT_METHODS else None,
        receipt=receipt if receipt in ("with", "without") else None,
        status=status if status in ("approved", "pending", "rejected") else None,
        trip_id=trip or None, shift_id=shift or None, driver_id=driver or None,
    )
    rows = await fl.expense_rows(session, owner.id, owner.timezone, flt)
    # Итоги и структура сверху — по «рамке» (период, а если открыт рейс или
    # смена — только их траты), но БЕЗ отборов: нажатая плашка или вид не
    # должны менять собственное число.
    facets_on = any([cat, vehicle, pay, receipt, status, driver])
    all_rows = rows if not facets_on else await fl.expense_rows(
        session, owner.id, owner.timezone,
        fl.ExpenseFilter(df, dt, trip_id=trip or None, shift_id=shift or None),
    )
    filters_on = facets_on or bool(trip or shift)

    # Расход к рейсу или смене: подставляем их в форму, чтобы запись легла
    # туда же, где на неё смотрят (правило «деньги записываются один раз»).
    prefill = None
    if trip:
        t = await session.get(Trip, trip)
        if t is not None and t.owner_id == owner.id:
            prefill = {"kind": "trip", "id": t.id, "vehicle_id": t.vehicle_id,
                       "title": f"К рейсу {t.origin or '—'} → {t.destination or '—'}"}
    elif shift:
        sh = await session.get(Shift, shift)
        if sh is not None and sh.owner_id == owner.id:
            prefill = {"kind": "shift", "id": sh.id, "vehicle_id": sh.vehicle_id,
                       "title": f"К смене №{sh.id}"}

    zone = _zone(owner)
    categories = await fl.all_categories(session, owner.id)
    # Откуда фото чека водителя — камера или галерея (владелец 17.09.2026).
    photo_origins = await driver_photos.origins(
        session, owner.id, [r.legacy_photo for r in rows if r.legacy_photo],
    )
    used = {r.category for r in all_rows if r.category}
    by_cat = {c["code"]: c for c in fl.by_category(all_rows)}
    chips = [
        {**c, "amount": by_cat.get(c["code"], {}).get("amount", 0.0),
         "share": by_cat.get(c["code"], {}).get("share", 0.0),
         "color": fl.category_color(c["code"])}
        for c in categories if c["code"] in used
    ]
    chips.sort(key=lambda c: c["amount"], reverse=True)
    drivers = (await session.execute(
        select(Driver).where(Driver.owner_id == owner.id).order_by(Driver.full_name)
    )).scalars().all()
    added_ids = {int(x) for x in (added or "").split(",") if x.isdigit()}
    return templates.TemplateResponse("fin_expenses.html", {
        "request": request, "owner": owner, "active_page": "finances",
        "fin_tab": "expenses", "counts": await _nav_counts(session, owner.id),
        "groups": _group_by_day(rows, zone), "zone": zone,
        "rows_count": len(rows), "photo_origins": photo_origins,
        "totals": fl.expense_totals(all_rows),
        "filtered_total": sum((r.amount for r in rows if r.status == "approved"), Decimal(0)),
        "chips": chips, "categories": categories,
        "payment_methods": fl.PAYMENT_METHODS,
        "vehicles": await _vehicles(session, owner.id),
        "drivers": drivers,
        "flt": {"cat": cat or "", "vehicle": vehicle or "", "pay": pay or "",
                "receipt": receipt or "", "status": status or "",
                "trip": trip or "", "shift": shift or "", "driver": driver or ""},
        "filtered": filters_on,
        "extra_filters": sum(1 for x in (vehicle, pay, receipt, status, driver) if x),
        "prefill": prefill, "open_new": bool(new),
        "added_ids": added_ids,
        "now_local": datetime.now(zone).strftime("%Y-%m-%dT%H:%M"),
        "category_colors": fl.CATEGORY_COLORS,
        **_period_context(df, dt),
    })


def _parse_spent_at(raw: str, zone: ZoneInfo) -> datetime | None:
    """Поле «дата и время» шлёт время без пояса — читаем его в поясе владельца."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        local = datetime.fromisoformat(raw)
    except ValueError:
        raise fl.LedgerError("Не понял дату. Выберите её в календаре.") from None
    if local.tzinfo is None:
        local = local.replace(tzinfo=zone)
    moment = local.astimezone(timezone.utc)
    if moment > datetime.now(timezone.utc) + timedelta(days=1):
        raise fl.LedgerError("Дата из будущего. Проверьте день.")
    return moment


def _int_or_none(raw) -> int | None:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@app.post("/finances/expenses")
async def finance_expenses_create(
    request: Request,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Несколько трат одним документом. Ответ — JSON: форма шлёт fetch'ем."""
    try:
        form = await request.form(
            max_files=fl.MAX_ITEMS * fl.MAX_ATTACHMENTS, max_fields=200,
        )
    except Exception:                                    # noqa: BLE001
        return JSONResponse({"ok": False, "message": "Форма не дошла. Попробуйте ещё раз."},
                            status_code=400)
    zone = _zone(owner)
    try:
        raw_items = json.loads(str(form.get("items") or "[]"))
        if not isinstance(raw_items, list):
            raise ValueError
    except ValueError:
        return JSONResponse({"ok": False, "message": "Форма собрана неверно."}, status_code=400)

    try:
        items: list[fl.NewExpense] = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise fl.LedgerError("Форма собрана неверно.")
            amount = fl.parse_amount(raw.get("amount"))
            if amount is None:
                raise fl.LedgerError(f"Трата №{index + 1}: укажите сумму больше нуля.")
            attachments = await _read_uploads(form, f"_{index}")
            items.append(fl.NewExpense(
                category=str(raw.get("category") or ""), amount=amount,
                description=str(raw.get("description") or "")[:500] or None,
                attachments=attachments,
            ))
        created = await fl.create_expenses(
            session, owner_id=owner.id, items=items,
            spent_at=_parse_spent_at(str(form.get("spent_at") or ""), zone),
            vehicle_id=_int_or_none(form.get("vehicle_id")) if form.get("link") == "vehicle" else None,
            payment_method=str(form.get("payment_method") or "") or None,
            supplier=str(form.get("supplier") or "") or None,
            trip_id=_int_or_none(form.get("trip_id")),
            shift_id=_int_or_none(form.get("shift_id")),
        )
    except fl.LedgerError as error:
        await session.rollback()
        return JSONResponse({"ok": False, "message": str(error)}, status_code=400)
    await session.commit()
    return JSONResponse({"ok": True, "ids": [e.id for e in created]})


@app.post("/finances/expenses/{expense_id}/delete")
async def finance_expense_delete(
    expense_id: int,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    if not await fl.delete_expense(session, owner.id, expense_id):
        raise HTTPException(status_code=404)
    await session.commit()
    return JSONResponse({"ok": True})


# В браузере открываем только обычные фото и PDF. ⚠️ SVG — тоже «картинка»,
# но в нём может быть скрипт: открытый прямо в кабинете, он выполнился бы от
# имени владельца. Всё прочее — только скачиванием, и в любом случае без
# права что-либо запускать (CSP sandbox).
_INLINE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic",
                 "application/pdf"}


def _attachment_response(att) -> Response:
    from urllib.parse import quote

    ctype = (att.content_type or "application/octet-stream").lower()
    inline = ctype in _INLINE_TYPES
    name = quote(att.filename or f"file-{att.id}")
    return Response(att.data, media_type=ctype if inline else "application/octet-stream", headers={
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{name}",
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox",
    })


@app.get("/finances/attachments/{attachment_id}")
async def finance_attachment(
    attachment_id: int,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    att = await session.get(ExpenseAttachment, attachment_id)
    if att is None or att.owner_id != owner.id:
        raise HTTPException(status_code=404)
    return _attachment_response(att)


@app.get("/finances/income-attachments/{attachment_id}")
async def finance_income_attachment(
    attachment_id: int,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    att = await session.get(IncomeAttachment, attachment_id)
    if att is None or att.owner_id != owner.id:
        raise HTTPException(status_code=404)
    return _attachment_response(att)


async def _read_uploads(form, suffix: str = "") -> list[fl.NewAttachment]:
    """Фото (`photos{suffix}`) и файлы (`files{suffix}`) из формы."""
    out: list[fl.NewAttachment] = []
    for field in ("photos", "files"):
        for upload in form.getlist(f"{field}{suffix}"):
            if isinstance(upload, str) or not getattr(upload, "filename", None):
                continue
            data = await upload.read(fl.MAX_ATTACHMENT_BYTES + 1)
            await upload.close()
            if not data:
                continue
            kind = "photo" if field == "photos" else fl.kind_of_upload(upload.content_type, upload.filename)
            out.append(fl.NewAttachment(kind, upload.filename, upload.content_type, data))
    return out


@app.post("/finances/categories")
async def finance_category_add(
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    name: Annotated[str, Form()] = "",
):
    try:
        code = await fl.add_category(session, owner.id, name)
    except ValueError as error:
        return JSONResponse({"ok": False, "message": str(error)}, status_code=400)
    await session.commit()
    return JSONResponse({"ok": True, "code": code, "label": fl.category_label(code)})


# ── Доходы ───────────────────────────────────────────────────────────────────

@app.get("/finances/income", response_class=HTMLResponse)
async def finance_income(
    request: Request,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    period_from: Annotated[str | None, Query()] = None,
    period_to: Annotated[str | None, Query()] = None,
    vehicle: Annotated[int | None, Query()] = None,
    added: Annotated[int | None, Query()] = None,
):
    df, dt = _parse_period(period_from, period_to)
    rows = await fl.income_rows(session, owner.id, owner.timezone, df, dt, vehicle_id=vehicle or None)
    zone = _zone(owner)
    trips = [r for r in rows if r.source == "trip"]
    manual = [r for r in rows if r.source == "manual"]
    flow = fl.cashflow(rows, [], df, dt, owner.timezone)
    return templates.TemplateResponse("fin_income.html", {
        "request": request, "owner": owner, "active_page": "finances",
        "fin_tab": "income", "counts": await _nav_counts(session, owner.id),
        "groups": _group_by_day(rows, zone), "zone": zone,
        "total": sum((r.amount for r in rows), Decimal(0)),
        "trips_total": sum((r.amount for r in trips), Decimal(0)),
        "trips_count": len(trips),
        "manual_total": sum((r.amount for r in manual), Decimal(0)),
        "vehicles": await _vehicles(session, owner.id),
        "flt": {"vehicle": vehicle or ""},
        "added_id": added, "today": datetime.now(zone).date().isoformat(),
        "charts_json": _script_json({"cashflow": flow}),
        **_period_context(df, dt),
    })


@app.post("/finances/income")
async def finance_income_create(
    request: Request,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Ручное поступление — с платёжкой, актом, фото (владелец 24.09.2026)."""
    try:
        form = await request.form(max_files=fl.MAX_ATTACHMENTS + 1, max_fields=50)
    except Exception:                                    # noqa: BLE001
        return JSONResponse({"ok": False, "message": "Форма не дошла. Попробуйте ещё раз."},
                            status_code=400)
    raw_day = str(form.get("entry_date") or "")
    try:
        day = date.fromisoformat(raw_day) if raw_day else datetime.now(_zone(owner)).date()
    except ValueError:
        return JSONResponse({"ok": False, "message": "Не понял дату."}, status_code=400)
    try:
        entry = await fl.create_income(
            session, owner_id=owner.id,
            amount=fl.parse_amount(form.get("amount")), day=day,
            category=str(form.get("category") or ""),
            description=str(form.get("description") or ""),
            attachments=await _read_uploads(form),
        )
    except fl.LedgerError as error:
        await session.rollback()
        return JSONResponse({"ok": False, "message": str(error)}, status_code=400)
    await session.commit()
    return JSONResponse({"ok": True, "id": entry.id})
