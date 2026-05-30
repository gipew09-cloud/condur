"""
Задачи APScheduler.

Все четыре джоба написаны как ASYNC-функции, которые сами:
  - открывают свою сессию БД через async_session;
  - принимают только owner_bot (для отправки уведомлений).

Запуск настраивается в app/main.py при старте процесса.
Параметры расписания: задачи дёргаются каждые N минут, и сами проверяют
текущее время в таймзоне КАЖДОГО владельца — потому что владельцы
могут быть из разных регионов (потенциально). Так не нужно регистрировать
отдельный cron-job на каждого.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from aiogram import Bot
from sqlalchemy import func, select

from app.bots import messages as msg
from app.bots.notifications import notify_owner
from app.database import async_session
from app.models import DailySummary, Driver, Event, Expense, Owner, Shift, Trip, Vehicle
from app.services.event_service import log_event
from app.services.timeutil import now_in_tz, owner_tz

logger = logging.getLogger(__name__)

# для детектора тишины: telegram_id водителя -> когда мы последний раз
# алертили владельца. Чтобы не спамить — не чаще 1 раз в 2 часа.
_SILENCE_LAST_ALERT: dict[int, datetime] = {}
SILENCE_THRESHOLD_HOURS = 4
SILENCE_DEDUP_HOURS = 2


# =========================================================================
# Дневная сводка — крутится каждые 30 мин, реагирует когда у владельца 21:0X
# =========================================================================
async def daily_summary_job(owner_bot: Bot) -> None:
    async with async_session() as session:
        owners_res = await session.execute(select(Owner))
        for owner in owners_res.scalars().all():
            local = now_in_tz(owner.timezone)
            if local.hour != 21 or local.minute >= 30:
                continue
            already = await session.execute(
                select(DailySummary).where(
                    DailySummary.owner_id == owner.id,
                    DailySummary.date == local.date(),
                )
            )
            if already.scalar_one_or_none() is not None:
                continue
            await _send_daily_summary(session, owner_bot, owner, local.date())


async def _send_daily_summary(
    session, owner_bot: Bot, owner: Owner, summary_date: date
) -> None:
    tz = owner_tz(owner.timezone)
    day_start_local = datetime.combine(summary_date, datetime.min.time()).replace(tzinfo=tz)
    day_end_local = day_start_local + timedelta(days=1)
    day_start = day_start_local.astimezone(timezone.utc)
    day_end = day_end_local.astimezone(timezone.utc)

    # активные сейчас смены
    active_shifts_res = await session.execute(
        select(Shift, Driver.full_name, Vehicle.license_plate)
        .join(Driver, Driver.id == Shift.driver_id)
        .join(Vehicle, Vehicle.id == Shift.vehicle_id)
        .where(Shift.owner_id == owner.id, Shift.status == "started")
    )
    active_shifts = list(active_shifts_res.all())

    # рейсы завершённые за день
    trips_res = await session.execute(
        select(func.count(Trip.id), func.coalesce(func.sum(Trip.revenue_rub), 0))
        .where(
            Trip.owner_id == owner.id,
            Trip.status == "completed",
            Trip.completed_at >= day_start,
            Trip.completed_at < day_end,
        )
    )
    trips_count, revenue = trips_res.one()
    revenue = Decimal(revenue or 0)

    expenses_res = await session.execute(
        select(func.coalesce(func.sum(Expense.amount_rub), 0))
        .where(
            Expense.owner_id == owner.id,
            Expense.status == "approved",
            Expense.created_at >= day_start,
            Expense.created_at < day_end,
        )
    )
    expenses = Decimal(expenses_res.scalar_one() or 0)

    pending_expenses_res = await session.execute(
        select(func.count(Expense.id))
        .where(Expense.owner_id == owner.id, Expense.status == "pending")
    )
    pending_count = pending_expenses_res.scalar_one() or 0

    lines = [msg.SUMMARY_HEADER.format(date=summary_date.strftime("%d.%m.%Y"))]
    lines.append(f"Завершённых рейсов: <b>{trips_count}</b>")
    lines.append(f"Выручка: <b>{revenue:.0f}</b> ₽")
    lines.append(f"Одобренных расходов: <b>{expenses:.0f}</b> ₽")
    lines.append(f"Прибыль (грубо): <b>{(revenue - expenses):.0f}</b> ₽")
    if active_shifts:
        lines.append("\n<b>Сейчас в смене:</b>")
        for sh, dname, plate in active_shifts:
            lines.append(f"• {dname} — {plate}")
    if pending_count:
        lines.append(f"\n⚠️ Расходов на одобрении: <b>{pending_count}</b>")

    summary = DailySummary(
        owner_id=owner.id,
        date=summary_date,
        total_trips=int(trips_count or 0),
        total_revenue=revenue,
        total_fuel_cost=Decimal(0),
    )
    session.add(summary)
    await session.commit()

    await notify_owner(owner_bot, session, owner, "\n".join(lines))


# =========================================================================
# Проверка истечения документов — каждые 30 мин, реагирует когда у владельца 09:0X
# =========================================================================
DOC_LABELS = {
    "osago_expires": "ОСАГО",
    "inspection_expires": "техосмотр",
    "tacho_expires": "поверка тахографа",
}


async def doc_expiry_job(owner_bot: Bot) -> None:
    async with async_session() as session:
        owners_res = await session.execute(select(Owner))
        for owner in owners_res.scalars().all():
            local = now_in_tz(owner.timezone)
            if local.hour != 9 or local.minute >= 30:
                continue
            await _check_owner_docs(session, owner_bot, owner, local.date())


async def _check_owner_docs(
    session, owner_bot: Bot, owner: Owner, today: date
) -> None:
    cutoff = today + timedelta(days=30)
    res = await session.execute(
        select(Vehicle).where(Vehicle.owner_id == owner.id, Vehicle.is_active.is_(True))
    )
    for vehicle in res.scalars().all():
        for field, label in DOC_LABELS.items():
            expires = getattr(vehicle, field)
            if expires is None:
                continue
            if expires < today:
                days_text = "истёк"
                days = (today - expires).days
                date_label = expires.strftime("%d.%m.%Y") + f" ({days} дн. назад)"
                await notify_owner(
                    owner_bot, session, owner,
                    f"🔴 У машины <b>{vehicle.license_plate}</b> {label} истёк "
                    f"{expires.strftime('%d.%m.%Y')} ({days} дн. назад).",
                )
                continue
            if expires <= cutoff:
                days = (expires - today).days
                await notify_owner(
                    owner_bot, session, owner,
                    msg.ALERT_DOC_EXPIRING.format(
                        plate=vehicle.license_plate, doc_label=label,
                        date_label=expires.strftime("%d.%m.%Y"), days=days,
                    ),
                )


# =========================================================================
# Late-start — каждые 15 минут, ищем кто должен был начать смену но не начал
# =========================================================================
async def late_start_job(owner_bot: Bot) -> None:
    async with async_session() as session:
        owners_res = await session.execute(select(Owner))
        for owner in owners_res.scalars().all():
            local_now = now_in_tz(owner.timezone)
            await _check_late_starts(session, owner_bot, owner, local_now)


async def _check_late_starts(
    session, owner_bot: Bot, owner: Owner, local_now: datetime
) -> None:
    drivers_res = await session.execute(
        select(Driver).where(
            Driver.owner_id == owner.id,
            Driver.is_active.is_(True),
            Driver.shift_start_time.is_not(None),
        )
    )
    today = local_now.date()
    for d in drivers_res.scalars().all():
        try:
            hh, mm = (int(x) for x in d.shift_start_time.split(":"))
        except (ValueError, AttributeError):
            continue
        expected = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # окно: водитель опоздал на 0..45 минут
        if not (timedelta(0) <= (local_now - expected) <= timedelta(minutes=45)):
            continue
        # есть ли активная смена за сегодня?
        shifts_res = await session.execute(
            select(Shift.id).where(
                Shift.driver_id == d.id,
                Shift.status == "started",
            )
        )
        if shifts_res.scalar_one_or_none() is not None:
            continue
        # уже алёртили сегодня?
        from sqlalchemy.dialects.postgresql import JSONB
        from app.models import Event
        existing_alert = await session.execute(
            select(Event.id).where(
                Event.owner_id == owner.id,
                Event.driver_id == d.id,
                Event.event_type == "late_start_alert",
                Event.created_at >= datetime.combine(today, datetime.min.time()).replace(
                    tzinfo=owner_tz(owner.timezone)
                ),
            )
        )
        if existing_alert.scalar_one_or_none() is not None:
            continue
        await log_event(
            session, owner_id=owner.id, driver_id=d.id,
            event_type="late_start_alert",
            payload={"expected": d.shift_start_time},
        )
        await session.commit()
        await notify_owner(
            owner_bot, session, owner,
            msg.ALERT_LATE_START.format(
                driver=d.full_name, expected_time=d.shift_start_time
            ),
        )


# =========================================================================
# Детектор тишины — каждые 30 минут проверяет все активные смены.
# Один SQL по всем водителям сразу, in-memory дедуп раз в 2ч на каждого.
# =========================================================================
async def silence_detector_job(owner_bot: Bot) -> None:
    threshold = datetime.now(timezone.utc) - timedelta(hours=SILENCE_THRESHOLD_HOURS)
    async with async_session() as session:
        # для каждого активного смена-водителя: последний event этого водителя
        result = await session.execute(
            select(
                Driver.id,
                Driver.full_name,
                Driver.owner_id,
                Driver.telegram_id,
                Shift.id.label("shift_id"),
                Shift.started_at,
                func.max(Event.created_at).label("last_event_at"),
            )
            .select_from(Shift)
            .join(Driver, Driver.id == Shift.driver_id)
            .outerjoin(Event, Event.driver_id == Driver.id)
            .where(Shift.status == "started")
            .group_by(
                Driver.id, Driver.full_name, Driver.owner_id,
                Driver.telegram_id, Shift.id, Shift.started_at,
            )
        )
        rows = list(result.all())
        if not rows:
            return

        owners_cache: dict[int, Owner] = {}
        now = datetime.now(timezone.utc)
        for driver_id, full_name, owner_id, _tid, shift_id, started_at, last_event_at in rows:
            last_seen = last_event_at or started_at
            if last_seen is None or last_seen > threshold:
                continue
            # дедуп
            previous_alert = _SILENCE_LAST_ALERT.get(driver_id)
            if previous_alert and (now - previous_alert) < timedelta(hours=SILENCE_DEDUP_HOURS):
                continue
            owner = owners_cache.get(owner_id)
            if owner is None:
                owner = await session.get(Owner, owner_id)
                if owner is None:
                    continue
                owners_cache[owner_id] = owner
            hours = (now - last_seen).total_seconds() / 3600
            text = (
                f"⚠️ <b>{full_name}</b> не выходит на связь {hours:.0f} ч.\n"
                f"Смена открыта с {started_at.astimezone(owner_tz(owner.timezone)):%H:%M %d.%m}."
            )
            await notify_owner(owner_bot, session, owner, text)
            _SILENCE_LAST_ALERT[driver_id] = now
            await log_event(
                session, owner_id=owner_id, driver_id=driver_id,
                shift_id=shift_id, event_type="silence_alert",
                payload={"hours_silent": round(hours, 1)},
            )
        await session.commit()
