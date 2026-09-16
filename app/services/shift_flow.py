"""
Начало и конец смены — ОДНА логика для бота, приложения и сайта.

Семь нейросетей сошлись (16.09.2026, `DRIVER_ACCESS_AI_ANSWERS.md`): если бот,
приложение и сайт по-своему понимают «смена закрыта», через год цифры начнут
расходиться. Поэтому здесь всё, что относится к самой смене: запись в базу,
отметка зажигания, событие в журнал и текст уведомления владельцу.
Как отвечать водителю и куда слать фото — решает тот, кто вызвал.

Коммит делает вызывающий.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import messages as msg
from app.models import Driver, Expense, Shift, Trip, Vehicle
from app.services import salary_service, shift_service, telemetry_service
from app.services.event_service import log_event


@dataclass
class OpenedShift:
    shift: Shift
    started_at: datetime
    ignition: dict | None


async def open_shift(
    session: AsyncSession,
    *,
    driver: Driver,
    vehicle: Vehicle,
    odometer_start: int | None,
    photo_ref: str | None,
    source: str,
    now: datetime | None = None,
) -> OpenedShift:
    """Открыть смену и записать это в журнал.

    `source` — откуда нажали: bot / app / web. Пишется в событие, чтобы потом
    было видно, откуда взялось действие.
    """
    shift = await shift_service.start_shift(
        session,
        owner_id=driver.owner_id,
        driver_id=driver.id,
        vehicle_id=vehicle.id,
        odometer_start=odometer_start,
        photo_file_id=photo_ref,
    )
    if now is not None:
        shift.started_at = now
    await session.flush()
    started_at = now or datetime.now(timezone.utc)
    # Зажигание на момент открытия смены: завели двигатель до смены или ещё
    # нет — уходит владельцу и в событие (GPS-точки живут 180 дней, события — вечно).
    ignition = await telemetry_service.shift_ignition_snapshot(
        session, vehicle_id=vehicle.id, moment=started_at
    )
    await log_event(
        session,
        owner_id=driver.owner_id,
        driver_id=driver.id,
        shift_id=shift.id,
        event_type="shift_started",
        payload={
            "vehicle_id": vehicle.id,
            "odometer_start": odometer_start,
            "engine_on": None if ignition is None else ignition["on"],
            "engine_since": ignition["since"].isoformat() if ignition else None,
            "source": source,
        },
    )
    return OpenedShift(shift=shift, started_at=started_at, ignition=ignition)


def shift_started_owner_text(
    opened: OpenedShift, *, driver: Driver, vehicle: Vehicle, tz_name: str | None,
) -> str:
    """Что уходит владельцу, когда водитель начал смену."""
    km = opened.shift.odometer_start
    if km is not None:
        text = msg.NOTIFY_SHIFT_STARTED.format(
            driver=driver.full_name, plate=vehicle.license_plate, km=km
        )
    else:
        text = msg.NOTIFY_SHIFT_STARTED_SIMPLE.format(
            driver=driver.full_name, plate=vehicle.license_plate
        )
    line = telemetry_service.ignition_shift_line(
        opened.ignition, moment=opened.started_at, tz_name=tz_name, closing=False
    )
    if line:
        text += f"\n{line}"
    return text


@dataclass
class ClosedShift:
    shift: Shift
    ended_at: datetime
    ignition: dict | None
    trips: list[Trip] = field(default_factory=list)
    revenue: Decimal = Decimal(0)
    pending_revenue: Decimal = Decimal(0)
    approved_expenses: list[Expense] = field(default_factory=list)
    expenses_total: Decimal = Decimal(0)
    salary: Decimal = Decimal(0)


async def close_shift(
    session: AsyncSession,
    *,
    driver: Driver,
    shift: Shift,
    odometer_end: int | None,
    photo_ref: str | None,
    source: str,
    now: datetime | None = None,
) -> ClosedShift:
    """Закрыть смену, посчитать итоги и записать в журнал."""
    ended_at = now or datetime.now(timezone.utc)
    await shift_service.end_shift(
        session,
        shift=shift,
        odometer_end=odometer_end,
        photo_file_id=photo_ref,
        ended_at=ended_at,
    )
    await session.flush()
    await session.refresh(shift)
    # Зажигание на момент закрытия: заглушил двигатель или уехал с работающим.
    ignition = await telemetry_service.shift_ignition_snapshot(
        session, vehicle_id=shift.vehicle_id, moment=ended_at
    )

    trips = await shift_service.get_shift_trips(session, shift.id)
    revenue = sum((t.revenue_rub or Decimal(0)) for t in trips) or Decimal(0)
    pending_revenue = (
        sum((t.driver_revenue_pending_rub or Decimal(0)) for t in trips) or Decimal(0)
    )
    approved = list((await session.execute(
        select(Expense).where(Expense.shift_id == shift.id, Expense.status == "approved")
    )).scalars().all())
    expenses_total = sum((e.amount_rub or Decimal(0)) for e in approved) or Decimal(0)
    salary = salary_service.calculate_salary(driver, shift, trips)

    await log_event(
        session,
        owner_id=driver.owner_id,
        driver_id=driver.id,
        shift_id=shift.id,
        event_type="shift_completed",
        payload={
            "distance_km": shift.distance_km,
            "trips": len(trips),
            "salary": str(salary),
            "engine_on": None if ignition is None else ignition["on"],
            "engine_since": ignition["since"].isoformat() if ignition else None,
            "source": source,
        },
    )
    return ClosedShift(
        shift=shift, ended_at=ended_at, ignition=ignition, trips=trips,
        revenue=revenue, pending_revenue=pending_revenue,
        approved_expenses=approved, expenses_total=expenses_total, salary=salary,
    )


def shift_completed_owner_text(
    closed: ClosedShift, *, driver: Driver, tz_name: str | None,
) -> str:
    """Что уходит владельцу, когда водитель закончил смену.

    Без показаний одометра пробег и зарплата ещё не известны — нули не пишем.
    """
    trips = closed.trips
    if closed.shift.odometer_end is None:
        text = msg.NOTIFY_SHIFT_COMPLETED_PENDING.format(
            driver=driver.full_name, trips=len(trips),
            revenue=f"{closed.revenue:.0f}", expenses=f"{closed.expenses_total:.0f}",
        )
    else:
        text = msg.NOTIFY_SHIFT_COMPLETED.format(
            driver=driver.full_name, distance=closed.shift.distance_km or 0,
            trips=len(trips), revenue=f"{closed.revenue:.0f}",
            expenses=f"{closed.expenses_total:.0f}", salary=f"{closed.salary:.0f}",
        )
    if closed.pending_revenue:
        text += f"\n⏳ Выручка на подтверждении: <b>{closed.pending_revenue:.0f} ₽</b>"
    # Честность цифры: если по части рейсов выручка ещё не вписана,
    # говорим это прямо — иначе «Рейсов: 3 · Выручка: 50000» выглядит
    # как ошибка, хотя просто не всё введено.
    no_revenue = sum(
        1 for t in trips
        if t.revenue_rub is None and t.driver_revenue_pending_rub is None
    )
    if no_revenue:
        text += (
            f"\n⚠️ По {no_revenue} из {len(trips)} рейс(ам) выручка ещё не "
            "указана — итог смены вырастет после ввода."
        )
    line = telemetry_service.ignition_shift_line(
        closed.ignition, moment=closed.ended_at, tz_name=tz_name, closing=True
    )
    if line:
        text += f"\n{line}"
    return text
