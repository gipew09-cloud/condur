"""
Веб-кабинет владельца.

Архитектура:
  - один FastAPI-app, который монтируется к asyncio.gather в main.py;
  - сессия БД даётся через Depends(get_session);
  - текущий владелец — через Depends(current_owner). На неавторизованных
    запросах кидаем RedirectResponse на /login;
  - все шаблоны Jinja2 рендерятся из app/web/templates;
  - HTMX определяется по заголовку HX-Request: для фильтра рейсов и
    редакта водителей возвращаем не полную страницу, а партиал.
"""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import and_, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import (
    Driver,
    Expense,
    ManualEntry,
    Owner,
    Shift,
    Trip,
    Vehicle,
)
from app.services import auth_service
from app.web.insights import generate_insights

# --------- инициализация ----------
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="TMS Cabinet")

# static (минимум — favicon/css)
static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# --------- зависимости ----------
async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session() as session:
        yield session


async def current_owner(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Owner:
    token = request.cookies.get("auth")
    if not token:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    owner_id = auth_service.decode_jwt(token)
    if owner_id is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    owner = await session.get(Owner, owner_id)
    if owner is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return owner


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Если кинули 303 с Location — редиректим. Иначе стандартное поведение."""
    if exc.status_code == 303 and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    from fastapi.exception_handlers import http_exception_handler as default_handler
    return await default_handler(request, exc)


# --------- утилиты ----------
def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _month_window(today: date | None = None) -> tuple[datetime, datetime]:
    today = today or date.today()
    start = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
    return start, datetime.now(timezone.utc)


def _today_window() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return start, now


# =========================================================================
# /login
# =========================================================================
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_submit(
    request: Request,
    telegram_id: Annotated[str, Form()],
    code: Annotated[str, Form()],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    try:
        tg_id = int(telegram_id.strip())
    except ValueError:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Telegram ID должен быть числом."},
            status_code=400,
        )

    if not auth_service.consume_code(tg_id, code):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный или истёкший код."},
            status_code=400,
        )

    result = await session.execute(select(Owner).where(Owner.telegram_id == tg_id))
    owner = result.scalar_one_or_none()
    if owner is None:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Владелец не найден. Сначала /start в боте."},
            status_code=400,
        )

    token = auth_service.create_jwt(owner.id)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "auth", token, httponly=True, samesite="lax", max_age=7 * 24 * 3600
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("auth")
    return response


# =========================================================================
# Dashboard
# =========================================================================
@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    today_start, today_end = _today_window()
    month_start, month_end = _month_window()

    trips_today = (
        await session.execute(
            select(func.count(Trip.id)).where(
                Trip.owner_id == owner.id,
                Trip.status == "completed",
                Trip.completed_at >= today_start,
            )
        )
    ).scalar_one() or 0

    km_today = (
        await session.execute(
            select(func.coalesce(func.sum(Shift.distance_km), 0)).where(
                Shift.owner_id == owner.id,
                Shift.status == "completed",
                Shift.ended_at >= today_start,
            )
        )
    ).scalar_one() or 0

    revenue_month = (
        await session.execute(
            select(func.coalesce(func.sum(Trip.revenue_rub), 0)).where(
                Trip.owner_id == owner.id,
                Trip.status == "completed",
                Trip.completed_at >= month_start,
            )
        )
    ).scalar_one() or Decimal(0)

    expenses_month_approved = (
        await session.execute(
            select(func.coalesce(func.sum(Expense.amount_rub), 0)).where(
                Expense.owner_id == owner.id,
                Expense.status == "approved",
                Expense.created_at >= month_start,
            )
        )
    ).scalar_one() or Decimal(0)

    fuel_month = (
        await session.execute(
            select(func.coalesce(func.sum(Trip.fuel_cost_rub), 0)).where(
                Trip.owner_id == owner.id,
                Trip.status == "completed",
                Trip.completed_at >= month_start,
            )
        )
    ).scalar_one() or Decimal(0)

    manual_income_month = (
        await session.execute(
            select(func.coalesce(func.sum(ManualEntry.amount_rub), 0)).where(
                ManualEntry.owner_id == owner.id,
                ManualEntry.type == "income",
                ManualEntry.entry_date >= month_start.date(),
            )
        )
    ).scalar_one() or Decimal(0)

    manual_expense_month = (
        await session.execute(
            select(func.coalesce(func.sum(ManualEntry.amount_rub), 0)).where(
                ManualEntry.owner_id == owner.id,
                ManualEntry.type == "expense",
                ManualEntry.entry_date >= month_start.date(),
            )
        )
    ).scalar_one() or Decimal(0)

    total_revenue = Decimal(revenue_month) + Decimal(manual_income_month)
    total_expenses = (
        Decimal(expenses_month_approved) + Decimal(fuel_month) + Decimal(manual_expense_month)
    )
    profit_month = total_revenue - total_expenses

    # последние 10 рейсов
    last_trips_res = await session.execute(
        select(Trip, Driver.full_name, Vehicle.license_plate)
        .join(Driver, Driver.id == Trip.driver_id)
        .join(Vehicle, Vehicle.id == Trip.vehicle_id)
        .where(Trip.owner_id == owner.id)
        .order_by(desc(Trip.created_at))
        .limit(10)
    )
    last_trips = list(last_trips_res.all())

    # график 7 дней (выручка завершённых рейсов и расходы из expenses+fuel)
    chart = await _seven_day_chart(session, owner.id)

    insights = await generate_insights(session, owner.id)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "owner": owner,
            "kpi": {
                "trips_today": trips_today,
                "km_today": km_today,
                "revenue_month": Decimal(revenue_month),
                "expenses_month": total_expenses,
                "profit_month": profit_month,
            },
            "last_trips": last_trips,
            "chart": chart,
            "insights": insights,
            "active_page": "dashboard",
        },
    )


async def _seven_day_chart(session: AsyncSession, owner_id: int) -> dict:
    """Готовим данные для Chart.js: labels + revenue + expenses по последним 7 дням."""
    end = date.today()
    start = end - timedelta(days=6)
    labels = []
    revenue_by_day: dict[date, Decimal] = {}
    expense_by_day: dict[date, Decimal] = {}

    rev_res = await session.execute(
        select(func.date(Trip.completed_at), func.coalesce(func.sum(Trip.revenue_rub), 0))
        .where(
            Trip.owner_id == owner_id,
            Trip.status == "completed",
            func.date(Trip.completed_at) >= start,
        )
        .group_by(func.date(Trip.completed_at))
    )
    for d, amount in rev_res.all():
        revenue_by_day[d] = Decimal(amount)

    exp_res = await session.execute(
        select(func.date(Expense.created_at), func.coalesce(func.sum(Expense.amount_rub), 0))
        .where(
            Expense.owner_id == owner_id,
            Expense.status == "approved",
            func.date(Expense.created_at) >= start,
        )
        .group_by(func.date(Expense.created_at))
    )
    for d, amount in exp_res.all():
        expense_by_day[d] = Decimal(amount)

    fuel_res = await session.execute(
        select(func.date(Trip.completed_at), func.coalesce(func.sum(Trip.fuel_cost_rub), 0))
        .where(
            Trip.owner_id == owner_id,
            Trip.status == "completed",
            func.date(Trip.completed_at) >= start,
        )
        .group_by(func.date(Trip.completed_at))
    )
    for d, amount in fuel_res.all():
        expense_by_day[d] = expense_by_day.get(d, Decimal(0)) + Decimal(amount)

    revenue = []
    expenses = []
    for i in range(7):
        d = start + timedelta(days=i)
        labels.append(d.strftime("%d.%m"))
        revenue.append(float(revenue_by_day.get(d, Decimal(0))))
        expenses.append(float(expense_by_day.get(d, Decimal(0))))
    return {"labels": labels, "revenue": revenue, "expenses": expenses}


# =========================================================================
# /trips
# =========================================================================
@app.get("/trips", response_class=HTMLResponse)
async def trips_page(
    request: Request,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    driver_id: Annotated[int | None, Query()] = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
):
    conditions = [Trip.owner_id == owner.id]
    if driver_id:
        conditions.append(Trip.driver_id == driver_id)
    if date_from:
        try:
            conditions.append(Trip.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            conditions.append(
                Trip.created_at < datetime.fromisoformat(date_to) + timedelta(days=1)
            )
        except ValueError:
            pass

    rows_res = await session.execute(
        select(Trip, Driver.full_name, Vehicle.license_plate)
        .join(Driver, Driver.id == Trip.driver_id)
        .join(Vehicle, Vehicle.id == Trip.vehicle_id)
        .where(and_(*conditions))
        .order_by(desc(Trip.created_at))
        .limit(200)
    )
    rows = list(rows_res.all())

    drivers_res = await session.execute(
        select(Driver).where(Driver.owner_id == owner.id).order_by(Driver.full_name)
    )
    drivers = list(drivers_res.scalars().all())

    ctx = {
        "request": request,
        "owner": owner,
        "rows": rows,
        "drivers": drivers,
        "filter_driver_id": driver_id,
        "filter_date_from": date_from or "",
        "filter_date_to": date_to or "",
        "active_page": "trips",
    }
    template = "_trips_table.html" if _is_htmx(request) else "trips.html"
    return templates.TemplateResponse(template, ctx)


# =========================================================================
# /drivers
# =========================================================================
@app.get("/drivers", response_class=HTMLResponse)
async def drivers_page(
    request: Request,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    rows = await _drivers_stats(session, owner.id)
    return templates.TemplateResponse(
        "drivers.html",
        {"request": request, "owner": owner, "rows": rows, "active_page": "drivers"},
    )


async def _drivers_stats(session: AsyncSession, owner_id: int) -> list[dict]:
    month_start, _ = _month_window()
    drivers_res = await session.execute(
        select(Driver).where(Driver.owner_id == owner_id).order_by(Driver.full_name)
    )
    drivers = list(drivers_res.scalars().all())

    rows = []
    for d in drivers:
        agg = await session.execute(
            select(
                func.coalesce(func.sum(Shift.distance_km), 0),
                func.count(Shift.id),
            ).where(
                Shift.driver_id == d.id,
                Shift.status == "completed",
                Shift.ended_at >= month_start,
            )
        )
        km, shifts_count = agg.one()
        trips_agg = await session.execute(
            select(
                func.count(Trip.id),
                func.coalesce(func.sum(Trip.revenue_rub), 0),
                func.coalesce(func.sum(Trip.fuel_cost_rub), 0),
            ).where(
                Trip.driver_id == d.id,
                Trip.status == "completed",
                Trip.completed_at >= month_start,
            )
        )
        trips_count, revenue, fuel_cost = trips_agg.one()
        rows.append({
            "driver": d,
            "km": km or 0,
            "shifts": shifts_count or 0,
            "trips": trips_count or 0,
            "revenue": Decimal(revenue or 0),
            "fuel_cost": Decimal(fuel_cost or 0),
        })
    return rows


@app.post("/drivers/{driver_id}/salary", response_class=HTMLResponse)
async def update_driver_salary(
    request: Request,
    driver_id: int,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    salary_type: Annotated[str, Form()],
    salary_rate: Annotated[str, Form()],
    per_diem_rub: Annotated[str, Form()] = "0",
):
    driver = await session.get(Driver, driver_id)
    if driver is None or driver.owner_id != owner.id:
        raise HTTPException(status_code=404, detail="Driver not found")
    if salary_type not in ("per_km", "per_trip", "percent", "fixed_per_shift"):
        raise HTTPException(status_code=400, detail="Bad salary_type")
    try:
        rate = Decimal(salary_rate.replace(",", "."))
        per_diem = Decimal(per_diem_rub.replace(",", "."))
        if rate < 0 or per_diem < 0:
            raise InvalidOperation
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Bad numeric values")

    driver.salary_type = salary_type
    driver.salary_rate = rate
    driver.per_diem_rub = per_diem
    await session.commit()

    # HTMX-парциал: одна строка таблицы
    rows = await _drivers_stats(session, owner.id)
    row = next((r for r in rows if r["driver"].id == driver.id), None)
    return templates.TemplateResponse(
        "_driver_row.html", {"request": request, "row": row, "edit": False}
    )


@app.get("/drivers/{driver_id}/edit", response_class=HTMLResponse)
async def driver_edit_form(
    request: Request,
    driver_id: int,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    driver = await session.get(Driver, driver_id)
    if driver is None or driver.owner_id != owner.id:
        raise HTTPException(status_code=404)
    rows = await _drivers_stats(session, owner.id)
    row = next((r for r in rows if r["driver"].id == driver.id), None)
    return templates.TemplateResponse(
        "_driver_row.html", {"request": request, "row": row, "edit": True}
    )


# =========================================================================
# /vehicles
# =========================================================================
@app.get("/vehicles", response_class=HTMLResponse)
async def vehicles_page(
    request: Request,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    month_start, _ = _month_window()
    vehicles_res = await session.execute(
        select(Vehicle)
        .where(Vehicle.owner_id == owner.id, Vehicle.is_active.is_(True))
        .order_by(Vehicle.license_plate)
    )
    vehicles = list(vehicles_res.scalars().all())

    rows = []
    for v in vehicles:
        agg = await session.execute(
            select(func.coalesce(func.sum(Shift.distance_km), 0)).where(
                Shift.vehicle_id == v.id,
                Shift.status == "completed",
                Shift.ended_at >= month_start,
            )
        )
        km = agg.scalar_one() or 0
        fuel = (
            await session.execute(
                select(func.coalesce(func.sum(Trip.fuel_cost_rub), 0)).where(
                    Trip.vehicle_id == v.id,
                    Trip.status == "completed",
                    Trip.completed_at >= month_start,
                )
            )
        ).scalar_one() or Decimal(0)
        trips_count = (
            await session.execute(
                select(func.count(Trip.id)).where(
                    Trip.vehicle_id == v.id,
                    Trip.status == "completed",
                    Trip.completed_at >= month_start,
                )
            )
        ).scalar_one() or 0
        rows.append({"vehicle": v, "km": km, "fuel": Decimal(fuel), "trips": trips_count})

    return templates.TemplateResponse(
        "vehicles.html",
        {"request": request, "owner": owner, "rows": rows, "active_page": "vehicles"},
    )


# =========================================================================
# /finances
# =========================================================================
@app.get("/finances", response_class=HTMLResponse)
async def finances_page(
    request: Request,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    period_from: Annotated[str | None, Query()] = None,
    period_to: Annotated[str | None, Query()] = None,
):
    df, dt = _parse_period(period_from, period_to)
    summary = await _finance_summary(session, owner.id, df, dt)

    entries_res = await session.execute(
        select(ManualEntry)
        .where(
            ManualEntry.owner_id == owner.id,
            ManualEntry.entry_date >= df,
            ManualEntry.entry_date <= dt,
        )
        .order_by(desc(ManualEntry.entry_date), desc(ManualEntry.id))
    )
    entries = list(entries_res.scalars().all())

    return templates.TemplateResponse(
        "finances.html",
        {
            "request": request,
            "owner": owner,
            "entries": entries,
            "summary": summary,
            "period_from": df.isoformat(),
            "period_to": dt.isoformat(),
            "today": date.today().isoformat(),
            "active_page": "finances",
        },
    )


@app.post("/finances/add", response_class=HTMLResponse)
async def finances_add(
    request: Request,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    type: Annotated[str, Form()],
    amount_rub: Annotated[str, Form()],
    entry_date: Annotated[str, Form()],
    category: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
):
    if type not in ("income", "expense"):
        raise HTTPException(status_code=400, detail="Bad type")
    try:
        amount = Decimal(amount_rub.replace(",", "."))
        if amount <= 0:
            raise InvalidOperation
        edate = date.fromisoformat(entry_date)
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail="Bad input")

    entry = ManualEntry(
        owner_id=owner.id,
        type=type,
        category=category.strip() or None,
        amount_rub=amount,
        description=description.strip() or None,
        entry_date=edate,
    )
    session.add(entry)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=400, detail="DB error")

    return templates.TemplateResponse(
        "_manual_entry_row.html", {"request": request, "entry": entry}
    )


@app.post("/finances/delete/{entry_id}")
async def finances_delete(
    entry_id: int,
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    entry = await session.get(ManualEntry, entry_id)
    if entry is None or entry.owner_id != owner.id:
        raise HTTPException(status_code=404)
    await session.delete(entry)
    await session.commit()
    return Response(status_code=200)


@app.get("/finances/export.xlsx")
async def finances_export(
    owner: Annotated[Owner, Depends(current_owner)],
    session: Annotated[AsyncSession, Depends(get_session)],
    period_from: Annotated[str | None, Query()] = None,
    period_to: Annotated[str | None, Query()] = None,
):
    df, dt = _parse_period(period_from, period_to)
    summary = await _finance_summary(session, owner.id, df, dt)

    wb = _build_finance_workbook(summary, df, dt)
    await _fill_entries_sheet(wb, session, owner.id, df, dt)
    await _fill_trips_sheet(wb, session, owner.id, df, dt)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"finances_{df.isoformat()}_{dt.isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------- Excel formatting helpers ---------
_HEADER_FILL = PatternFill("solid", fgColor="305496")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_MONEY_FMT = "#,##0 ₽"
_DATE_FMT = "DD.MM.YYYY"


def _style_header(ws, ncols: int) -> None:
    for col_idx in range(1, ncols + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"


def _autosize(ws) -> None:
    for col_idx, col in enumerate(ws.columns, start=1):
        max_len = 0
        for cell in col:
            value = cell.value
            if value is None:
                continue
            text = str(value)
            if len(text) > max_len:
                max_len = len(text)
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(max_len + 2, 10), 50)


def _build_finance_workbook(summary: dict, df: date, dt: date) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Итог"
    ws.append(["Показатель", "Сумма"])
    rows = [
        ("Период", f"{df.strftime('%d.%m.%Y')} — {dt.strftime('%d.%m.%Y')}"),
        ("Выручка по рейсам", float(summary["trip_revenue"])),
        ("Ручной доход", float(summary["manual_income"])),
        ("Топливо по рейсам", float(summary["fuel"])),
        ("Одобренные расходы водителей", float(summary["driver_expenses"])),
        ("Ручной расход", float(summary["manual_expense"])),
        ("Итого выручка", float(summary["total_income"])),
        ("Итого расход", float(summary["total_expense"])),
        ("Прибыль", float(summary["profit"])),
    ]
    for label, value in rows:
        ws.append([label, value])
    _style_header(ws, 2)
    for row_idx in range(2, ws.max_row + 1):
        cell = ws.cell(row=row_idx, column=2)
        if isinstance(cell.value, (int, float)):
            cell.number_format = _MONEY_FMT
            cell.alignment = Alignment(horizontal="right")
    # выделим строку «Прибыль»
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=2).font = Font(bold=True)
    _autosize(ws)
    return wb


async def _fill_entries_sheet(wb: Workbook, session, owner_id: int, df: date, dt: date) -> None:
    ws = wb.create_sheet("Ручные записи")
    ws.append(["Дата", "Тип", "Категория", "Сумма", "Описание"])
    entries_res = await session.execute(
        select(ManualEntry)
        .where(
            ManualEntry.owner_id == owner_id,
            ManualEntry.entry_date >= df,
            ManualEntry.entry_date <= dt,
        )
        .order_by(ManualEntry.entry_date)
    )
    for e in entries_res.scalars().all():
        ws.append([
            e.entry_date,
            "Доход" if e.type == "income" else "Расход",
            e.category or "",
            float(e.amount_rub),
            e.description or "",
        ])
    _style_header(ws, 5)
    for row_idx in range(2, ws.max_row + 1):
        ws.cell(row=row_idx, column=1).number_format = _DATE_FMT
        amount_cell = ws.cell(row=row_idx, column=4)
        amount_cell.number_format = _MONEY_FMT
        amount_cell.alignment = Alignment(horizontal="right")
    _autosize(ws)


async def _fill_trips_sheet(wb: Workbook, session, owner_id: int, df: date, dt: date) -> None:
    ws = wb.create_sheet("Рейсы")
    ws.append(["Дата", "Маршрут", "Водитель", "Машина", "Выручка", "Топливо", "Прибыль"])
    trips_res = await session.execute(
        select(Trip, Driver.full_name, Vehicle.license_plate)
        .join(Driver, Driver.id == Trip.driver_id)
        .join(Vehicle, Vehicle.id == Trip.vehicle_id)
        .where(
            Trip.owner_id == owner_id,
            Trip.status == "completed",
            func.date(Trip.completed_at) >= df,
            func.date(Trip.completed_at) <= dt,
        )
        .order_by(Trip.completed_at)
    )
    for trip, driver_name, plate in trips_res.all():
        ws.append([
            trip.completed_at.date() if trip.completed_at else None,
            f"{trip.origin or ''} → {trip.destination or ''}",
            driver_name,
            plate,
            float(trip.revenue_rub or 0),
            float(trip.fuel_cost_rub or 0),
            float(trip.profit_rub or 0),
        ])
    _style_header(ws, 7)
    for row_idx in range(2, ws.max_row + 1):
        ws.cell(row=row_idx, column=1).number_format = _DATE_FMT
        for col_idx in (5, 6, 7):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.number_format = _MONEY_FMT
            cell.alignment = Alignment(horizontal="right")
    _autosize(ws)


def _parse_period(period_from: str | None, period_to: str | None) -> tuple[date, date]:
    today = date.today()
    default_from = today.replace(day=1)
    df = today.replace(day=1)
    dt = today
    if period_from:
        try:
            df = date.fromisoformat(period_from)
        except ValueError:
            df = default_from
    if period_to:
        try:
            dt = date.fromisoformat(period_to)
        except ValueError:
            dt = today
    if dt < df:
        df, dt = dt, df
    return df, dt


async def _finance_summary(
    session: AsyncSession, owner_id: int, df: date, dt: date
) -> dict:
    trip_revenue = (
        await session.execute(
            select(func.coalesce(func.sum(Trip.revenue_rub), 0)).where(
                Trip.owner_id == owner_id,
                Trip.status == "completed",
                func.date(Trip.completed_at) >= df,
                func.date(Trip.completed_at) <= dt,
            )
        )
    ).scalar_one() or Decimal(0)
    fuel = (
        await session.execute(
            select(func.coalesce(func.sum(Trip.fuel_cost_rub), 0)).where(
                Trip.owner_id == owner_id,
                Trip.status == "completed",
                func.date(Trip.completed_at) >= df,
                func.date(Trip.completed_at) <= dt,
            )
        )
    ).scalar_one() or Decimal(0)
    driver_expenses = (
        await session.execute(
            select(func.coalesce(func.sum(Expense.amount_rub), 0)).where(
                Expense.owner_id == owner_id,
                Expense.status == "approved",
                func.date(Expense.created_at) >= df,
                func.date(Expense.created_at) <= dt,
            )
        )
    ).scalar_one() or Decimal(0)
    manual_income = (
        await session.execute(
            select(func.coalesce(func.sum(ManualEntry.amount_rub), 0)).where(
                ManualEntry.owner_id == owner_id,
                ManualEntry.type == "income",
                ManualEntry.entry_date >= df,
                ManualEntry.entry_date <= dt,
            )
        )
    ).scalar_one() or Decimal(0)
    manual_expense = (
        await session.execute(
            select(func.coalesce(func.sum(ManualEntry.amount_rub), 0)).where(
                ManualEntry.owner_id == owner_id,
                ManualEntry.type == "expense",
                ManualEntry.entry_date >= df,
                ManualEntry.entry_date <= dt,
            )
        )
    ).scalar_one() or Decimal(0)

    total_income = Decimal(trip_revenue) + Decimal(manual_income)
    total_expense = Decimal(fuel) + Decimal(driver_expenses) + Decimal(manual_expense)
    return {
        "trip_revenue": Decimal(trip_revenue),
        "fuel": Decimal(fuel),
        "driver_expenses": Decimal(driver_expenses),
        "manual_income": Decimal(manual_income),
        "manual_expense": Decimal(manual_expense),
        "total_income": total_income,
        "total_expense": total_expense,
        "profit": total_income - total_expense,
    }


# =========================================================================
# Health
# =========================================================================
@app.get("/health")
async def health():
    return {"status": "ok"}
