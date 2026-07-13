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
from decimal import Decimal, InvalidOperation

from aiogram import Bot
from sqlalchemy import func, select

from app.bots import messages as msg
from app.bots.notifications import notify_owner
from app.database import async_session
from app.models import (
    DailySummary, DistributionCenter, Driver, Event, Expense, Owner, Shift, Trip,
    Vehicle, VehicleState,
)
from app.services import rc_service, telemetry_service
from app.services.event_service import log_event
from app.services.timeutil import fmt_dt, now_in_tz, owner_tz

logger = logging.getLogger(__name__)

# Детектор тишины: не спамить — не чаще 1 раза в SILENCE_DEDUP_HOURS на
# водителя. Дедуп теперь через таблицу Event (см. silence_detector_job),
# поэтому in-memory словарь больше не нужен.
SILENCE_THRESHOLD_HOURS = 4
SILENCE_DEDUP_HOURS = 2


# =========================================================================
# Дневная сводка — крутится каждые 30 мин, реагирует когда у владельца 21:0X
# =========================================================================
async def daily_summary_job(owner_bot: Bot) -> None:
    async with async_session() as session:
        owners_res = await session.execute(select(Owner))
        for owner in owners_res.scalars().all():
            try:
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
            except Exception:
                logger.exception("daily_summary_job failed for owner %s", owner.id)
                await session.rollback()
                continue


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
    active_shift_vehicle_ids = {sh.vehicle_id for sh, _dname, _plate in active_shifts}
    active_trip_vehicle_ids = set(
        (
            await session.execute(
                select(Trip.vehicle_id).where(
                    Trip.owner_id == owner.id,
                    Trip.status.in_(("created", "in_transit", "unloading")),
                )
            )
        ).scalars().all()
    )

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

    # GPS-контроль: полезные сигналы владельцу без лишнего шума.
    gps_rows = (
        await session.execute(
            select(VehicleState, Vehicle.license_plate)
            .join(Vehicle, Vehicle.id == VehicleState.vehicle_id)
            .where(Vehicle.owner_id == owner.id, Vehicle.is_active.is_(True))
        )
    ).all()
    now_utc = datetime.now(timezone.utc)
    stale_cutoff = now_utc - timedelta(minutes=30)
    gps_lines: list[str] = []
    for st, plate in gps_rows:
        status = st.motion_status or telemetry_service.vehicle_motion_status(st.speed_kmh, st.ignition)
        signal = telemetry_service.vehicle_control_signal(
            motion_status=status,
            has_active_shift=st.vehicle_id in active_shift_vehicle_ids,
            has_active_trip=st.vehicle_id in active_trip_vehicle_ids,
            gps_stale=bool(st.last_seen_at and st.last_seen_at < stale_cutoff),
            gps_invalid=st.is_valid is False,
        )
        if signal == telemetry_service.SIGNAL_MOVING_WITHOUT_SHIFT:
            gps_lines.append(
                f"• {plate}: едет без смены "
                f"({Decimal(st.speed_kmh or 0):.0f} км/ч, с {fmt_dt(st.motion_since_at, owner.timezone, '%H:%M')})"
            )
        elif signal == telemetry_service.SIGNAL_MOVING_WITHOUT_TRIP:
            gps_lines.append(
                f"• {plate}: едет без активного рейса "
                f"({Decimal(st.speed_kmh or 0):.0f} км/ч, с {fmt_dt(st.motion_since_at, owner.timezone, '%H:%M')})"
            )
        elif signal == telemetry_service.SIGNAL_IDLE_ENGINE:
            gps_lines.append(
                f"• {plate}: стоит с заведённым с "
                f"{fmt_dt(st.motion_since_at, owner.timezone, '%H:%M')} "
                f"({telemetry_service.duration_label(st.motion_since_at, now_utc)})"
            )
        elif signal == telemetry_service.SIGNAL_GPS_STALE:
            gps_lines.append(
                f"• {plate}: нет GPS {telemetry_service.duration_label(st.last_seen_at, now_utc)}"
            )
        elif signal == telemetry_service.SIGNAL_GPS_INVALID:
            gps_lines.append(f"• {plate}: GPS без точных координат")
    if gps_lines:
        lines.append("\n<b>GPS-контроль:</b>")
        lines.extend(gps_lines[:5])

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
            try:
                local = now_in_tz(owner.timezone)
                if local.hour != 9 or local.minute >= 30:
                    continue
                await _check_owner_docs(session, owner_bot, owner, local.date())
            except Exception:
                logger.exception("doc_expiry_job failed for owner %s", owner.id)
                await session.rollback()
                continue


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
            try:
                local_now = now_in_tz(owner.timezone)
                await _check_late_starts(session, owner_bot, owner, local_now)
            except Exception:
                logger.exception("late_start_job failed for owner %s", owner.id)
                await session.rollback()
                continue


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
# Один SQL по всем водителям сразу, дедуп раз в 2ч на каждого через
# таблицу Event (переживает рестарт и несколько процессов).
# =========================================================================
async def silence_detector_job(owner_bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=SILENCE_THRESHOLD_HOURS)
    dedup_threshold = now - timedelta(hours=SILENCE_DEDUP_HOURS)
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

        # Дедуп через Event: кому уже слали silence_alert за последние
        # SILENCE_DEDUP_HOURS — пропускаем (раньше держали в памяти).
        active_driver_ids = [r[0] for r in rows]
        recent_res = await session.execute(
            select(Event.driver_id).where(
                Event.event_type == "silence_alert",
                Event.driver_id.in_(active_driver_ids),
                Event.created_at >= dedup_threshold,
            )
        )
        recently_alerted = {row[0] for row in recent_res.all()}

        owners_cache: dict[int, Owner] = {}
        for driver_id, full_name, owner_id, _tid, shift_id, started_at, last_event_at in rows:
            last_seen = last_event_at or started_at
            if last_seen is None or last_seen > threshold:
                continue
            if driver_id in recently_alerted:
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
            recently_alerted.add(driver_id)  # защита от дублей в этом же прогоне
            await log_event(
                session, owner_id=owner_id, driver_id=driver_id,
                shift_id=shift_id, event_type="silence_alert",
                payload={"hours_silent": round(hours, 1)},
            )
        await session.commit()


# =========================================================================
# Еженедельный разбор — воскресенье 21:30 локального времени владельца.
# Нарочно ПОСЛЕ дневной сводки (21:00): сначала итог дня, потом итог недели.
# =========================================================================
async def weekly_review_job(owner_bot: Bot) -> None:
    async with async_session() as session:
        owners_res = await session.execute(select(Owner))
        for owner in owners_res.scalars().all():
            try:
                local = now_in_tz(owner.timezone)
                # воскресенье = weekday() == 6, окно 21:30..21:59
                # (дневная сводка уходит в 21:00..21:29 — недельная строго после неё)
                if local.weekday() != 6 or local.hour != 21 or local.minute < 30:
                    continue
                # уже отправляли сегодня?
                already = await session.execute(
                    select(Event.id).where(
                        Event.owner_id == owner.id,
                        Event.event_type == "weekly_review_sent",
                        Event.created_at >= datetime.combine(
                            local.date(), datetime.min.time()
                        ).replace(tzinfo=owner_tz(owner.timezone)),
                    )
                )
                if already.scalar_one_or_none() is not None:
                    continue
                await _send_weekly_review(session, owner_bot, owner, local)
            except Exception:
                logger.exception("weekly_review_job failed for owner %s", owner.id)
                await session.rollback()
                continue


async def _send_weekly_review(
    session, owner_bot: Bot, owner: Owner, local_now: datetime
) -> None:
    """
    Период «неделя» = последние 7 дней (включая сегодня).
    Сравнение с предыдущей неделей: 7..13 дней назад.
    """
    tz = owner_tz(owner.timezone)
    week_end = local_now
    week_start = (week_end - timedelta(days=7)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    prev_week_end = week_start
    prev_week_start = week_start - timedelta(days=7)

    def _utc(dt):
        return dt.astimezone(timezone.utc)

    # Прибыль текущей и прошлой недели
    cur_profit = await _sum_profit(session, owner.id, _utc(week_start), _utc(week_end))
    prev_profit = await _sum_profit(session, owner.id, _utc(prev_week_start), _utc(prev_week_end))

    # Топ-3 прибыльных и убыточных рейса за неделю
    trips_res = await session.execute(
        select(Trip, Driver.full_name)
        .join(Driver, Driver.id == Trip.driver_id)
        .where(
            Trip.owner_id == owner.id,
            Trip.status == "completed",
            Trip.completed_at >= _utc(week_start),
            Trip.completed_at <= _utc(week_end),
            Trip.revenue_rub.is_not(None),
        )
    )
    trips_with_profit = []
    for trip, driver_name in trips_res.all():
        if trip.profit_rub is None:
            continue
        trips_with_profit.append((trip, driver_name, Decimal(trip.profit_rub)))
    trips_with_profit.sort(key=lambda x: x[2], reverse=True)
    top_profitable = trips_with_profit[:3]
    top_loss = sorted(trips_with_profit, key=lambda x: x[2])[:3]

    # Водитель с самым высоким расходом топлива
    fuel_res = await session.execute(
        select(
            Driver.full_name,
            func.coalesce(func.sum(Expense.amount_rub), 0).label("fuel_sum"),
        )
        .select_from(Expense)
        .join(Driver, Driver.id == Expense.driver_id)
        .where(
            Expense.owner_id == owner.id,
            Expense.category == "fuel",
            Expense.status == "approved",
            Expense.created_at >= _utc(week_start),
            Expense.created_at <= _utc(week_end),
        )
        .group_by(Driver.full_name)
        .order_by(func.sum(Expense.amount_rub).desc())
        .limit(1)
    )
    fuel_winner = fuel_res.first()

    lines = ["📅 <b>Итоги недели</b>\n"]
    lines.append(
        f"Прибыль: <b>{cur_profit:,.0f} ₽</b>".replace(",", " ") +
        (
            f" ({'↑' if cur_profit > prev_profit else '↓'} {abs(cur_profit - prev_profit):,.0f} ₽ vs прошлая)".replace(",", " ")
            if prev_profit != 0 or cur_profit != 0 else ""
        )
    )
    if top_profitable:
        lines.append("\n🏆 <b>Самые прибыльные:</b>")
        for t, name, profit in top_profitable:
            lines.append(f"  • {t.origin} → {t.destination} ({name}) — {profit:,.0f} ₽".replace(",", " "))
    if top_loss and top_loss[0][2] < 0:
        lines.append("\n📉 <b>Убыточные:</b>")
        for t, name, profit in top_loss:
            if profit >= 0:
                break
            lines.append(f"  • {t.origin} → {t.destination} ({name}) — {profit:,.0f} ₽".replace(",", " "))
    if fuel_winner:
        fuel_name, fuel_sum = fuel_winner
        lines.append(f"\n⛽️ Больше всего на топливо: <b>{fuel_name}</b> ({Decimal(fuel_sum):,.0f} ₽)".replace(",", " "))

    await log_event(
        session, owner_id=owner.id, event_type="weekly_review_sent",
        payload={"profit": str(cur_profit)},
    )
    await session.commit()
    await notify_owner(owner_bot, session, owner, "\n".join(lines))


async def _sum_profit(session, owner_id: int, dt_from, dt_to) -> Decimal:
    """Прибыль за период = выручка рейсов − одобренные расходы."""
    revenue = (
        await session.execute(
            select(func.coalesce(func.sum(Trip.revenue_rub), 0)).where(
                Trip.owner_id == owner_id,
                Trip.status == "completed",
                Trip.completed_at >= dt_from,
                Trip.completed_at <= dt_to,
            )
        )
    ).scalar_one() or Decimal(0)
    expenses = (
        await session.execute(
            select(func.coalesce(func.sum(Expense.amount_rub), 0)).where(
                Expense.owner_id == owner_id,
                Expense.status == "approved",
                Expense.created_at >= dt_from,
                Expense.created_at <= dt_to,
            )
        )
    ).scalar_one() or Decimal(0)
    return Decimal(revenue) - Decimal(expenses)


# =========================================================================
# Экономометр — 1 числа каждого месяца, 10:00 локально
# Считает «сколько денег система помогла увидеть» за прошлый месяц:
#   - сумма перерасходов топлива из event 'fuel_overrun_alert'
#   - сумма отклонённых расходов
#   - прибыль за месяц
# =========================================================================
async def monthly_econometer_job(owner_bot: Bot) -> None:
    async with async_session() as session:
        owners_res = await session.execute(select(Owner))
        for owner in owners_res.scalars().all():
            try:
                local = now_in_tz(owner.timezone)
                if local.day != 1 or local.hour != 10 or local.minute >= 30:
                    continue
                already = await session.execute(
                    select(Event.id).where(
                        Event.owner_id == owner.id,
                        Event.event_type == "econometer_sent",
                        Event.created_at >= datetime.combine(
                            local.date(), datetime.min.time()
                        ).replace(tzinfo=owner_tz(owner.timezone)),
                    )
                )
                if already.scalar_one_or_none() is not None:
                    continue
                await _send_econometer(session, owner_bot, owner, local)
            except Exception:
                logger.exception("monthly_econometer_job failed for owner %s", owner.id)
                await session.rollback()
                continue


async def _send_econometer(session, owner_bot: Bot, owner: Owner, local_now: datetime) -> None:
    tz = owner_tz(owner.timezone)
    # прошлый месяц
    first_of_this = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_of_prev = first_of_this - timedelta(seconds=1)
    first_of_prev = last_of_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _utc(d):
        return d.astimezone(timezone.utc)

    # 1. Перерасходы топлива
    overruns_res = await session.execute(
        select(Event.payload).where(
            Event.owner_id == owner.id,
            Event.event_type == "fuel_overrun_alert",
            Event.created_at >= _utc(first_of_prev),
            Event.created_at <= _utc(last_of_prev),
        )
    )
    overrun_total = Decimal(0)
    for (payload,) in overruns_res.all():
        if payload and "excess_rub" in payload:
            try:
                overrun_total += Decimal(payload["excess_rub"])
            except (InvalidOperation, TypeError):
                pass

    # 2. Отклонённые расходы
    rejected = (
        await session.execute(
            select(func.coalesce(func.sum(Expense.amount_rub), 0)).where(
                Expense.owner_id == owner.id,
                Expense.status == "rejected",
                Expense.decided_at >= _utc(first_of_prev),
                Expense.decided_at <= _utc(last_of_prev),
            )
        )
    ).scalar_one() or Decimal(0)

    # 3. Прибыль за прошлый месяц
    profit = await _sum_profit(session, owner.id, _utc(first_of_prev), _utc(last_of_prev))

    saved = overrun_total + Decimal(rejected)
    month_label = last_of_prev.strftime("%B %Y").lower()
    text = (
        f"📈 <b>Итоги {month_label}</b>\n\n"
        f"Прибыль за месяц: <b>{profit:,.0f} ₽</b>\n\n".replace(",", " ") +
        f"Система помогла увидеть ~<b>{saved:,.0f} ₽</b>:".replace(",", " ") + "\n"
        f"• Перерасход топлива: {overrun_total:,.0f} ₽\n".replace(",", " ") +
        f"• Отклонённые расходы: {Decimal(rejected):,.0f} ₽\n".replace(",", " ") +
        "\n<i>Это сумма, которую вы успели проконтролировать благодаря автоматическим алертам и проверкам.</i>"
    )
    await log_event(
        session, owner_id=owner.id, event_type="econometer_sent",
        payload={"saved": str(saved), "profit": str(profit)},
    )
    await session.commit()
    await notify_owner(owner_bot, session, owner, text)


# =========================================================================
# Невыход водителя — каждый час. Активный водитель без смен дольше
# NO_SHOW_THRESHOLD_HOURS → уведомить владельца (дедуп раз в сутки через Event).
# =========================================================================
NO_SHOW_THRESHOLD_HOURS = 36


async def no_show_detector_job(owner_bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=NO_SHOW_THRESHOLD_HOURS)
    async with async_session() as session:
        # Тянем поля водителей сразу значениями: rollback внутри цикла помечает
        # ORM-объекты «протухшими», и следующее обращение к driver.id пыталось
        # бы сходить в БД ленивой (синхронной) загрузкой — это роняло job.
        drivers_res = await session.execute(
            select(Driver.id, Driver.owner_id, Driver.full_name, Driver.created_at)
            .where(Driver.is_active.is_(True))
        )
        for driver_id, owner_id, full_name, created_at in drivers_res.all():
            try:
                # есть активная смена? тогда это не невыход.
                # limit(1): у водителя может «висеть» несколько открытых смен.
                active = await session.execute(
                    select(Shift.id)
                    .where(Shift.driver_id == driver_id, Shift.status == "started")
                    .limit(1)
                )
                if active.scalars().first() is not None:
                    continue
                # последняя активность по сменам (или дата подключения водителя)
                last_shift = await session.execute(
                    select(func.max(Shift.started_at)).where(Shift.driver_id == driver_id)
                )
                last_active = last_shift.scalar_one() or created_at
                if last_active is None or last_active > threshold:
                    continue
                # Одно уведомление на один простой: если уже алёртили ПОСЛЕ его
                # последней смены — молчим (иначе про тестовых/уволившихся
                # водителей сыпалось каждый день). Выйдет на смену — счётчик
                # сам сдвинется, и новый простой снова сможет уведомить.
                # limit(1): алёртов могло накопиться несколько.
                already = await session.execute(
                    select(Event.id)
                    .where(
                        Event.driver_id == driver_id,
                        Event.event_type == "no_show_alert",
                        Event.created_at >= last_active,
                    )
                    .limit(1)
                )
                if already.scalars().first() is not None:
                    continue
                owner = await session.get(Owner, owner_id)
                if owner is None:
                    continue
                since_label = last_active.astimezone(
                    owner_tz(owner.timezone)
                ).strftime("%d.%m %H:%M")
                hours = int((now - last_active).total_seconds() // 3600)
                await notify_owner(
                    owner_bot, session, owner,
                    f"🚷 <b>{full_name}</b> не выходил на смену с {since_label} "
                    f"(~{hours} ч). Возможно, простаивает — есть кому дать работу?",
                )
                await log_event(
                    session, owner_id=owner.id, driver_id=driver_id,
                    event_type="no_show_alert", payload={"hours": hours},
                )
                await session.commit()
            except Exception:
                logger.exception("no_show_detector_job failed for driver %s", driver_id)
                await session.rollback()
                continue


# =========================================================================
# GPS-детектор «поехала не та машина» — каждые ~10 минут.
# Ситуация-миссклик: водитель открыл смену на машине X, но сел в машину Y.
# Признак: машина смены стоит > N минут, а другая машина БЕЗ смены едет.
# Заодно общий алерт «движение без смены» (машина едет, смены нет вообще).
# Дедуп через Event: не чаще раза в MIXUP_DEDUP_HOURS на пару машин.
# =========================================================================
MIXUP_SHIFT_STOPPED_MINUTES = 15
MIXUP_DEDUP_HOURS = 2
MIXUP_GPS_STALE_MINUTES = 30
# Напоминание водителю «поехал — начни смену»: не чаще раза в N часов на машину,
# и только если машина едет уже не мельком (фильтр от кратких скачков GPS).
START_REMINDER_DEDUP_HOURS = 2
START_REMINDER_MIN_MOVING_MINUTES = 3


async def vehicle_mixup_detector_job(owner_bot: Bot, driver_bot: Bot | None = None) -> None:
    async with async_session() as session:
        owners_res = await session.execute(select(Owner))
        for owner in owners_res.scalars().all():
            try:
                await _check_vehicle_mixup(session, owner_bot, owner, driver_bot)
            except Exception:
                logger.exception("vehicle_mixup_detector_job failed for owner %s", owner.id)
                await session.rollback()
                continue


async def _check_vehicle_mixup(
    session, owner_bot: Bot, owner: Owner, driver_bot: Bot | None = None
) -> None:
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(minutes=MIXUP_GPS_STALE_MINUTES)
    stopped_cutoff = now - timedelta(minutes=MIXUP_SHIFT_STOPPED_MINUTES)
    dedup_since = now - timedelta(hours=MIXUP_DEDUP_HOURS)

    # активные смены: vehicle_id → (shift_id, имя водителя, номер машины)
    shifts_res = await session.execute(
        select(Shift.id, Shift.vehicle_id, Driver.full_name, Vehicle.license_plate)
        .join(Driver, Driver.id == Shift.driver_id)
        .join(Vehicle, Vehicle.id == Shift.vehicle_id)
        .where(Shift.owner_id == owner.id, Shift.status == "started")
    )
    shift_by_vehicle = {
        vehicle_id: (shift_id, driver_name, plate)
        for shift_id, vehicle_id, driver_name, plate in shifts_res.all()
    }
    # водители, у кого сейчас есть активная смена (на любой машине) — им
    # напоминание «начни смену» не шлём
    active_driver_ids = {
        row[0]
        for row in (
            await session.execute(
                select(Shift.driver_id).where(
                    Shift.owner_id == owner.id, Shift.status == "started"
                )
            )
        ).all()
    }

    # свежие GPS-состояния всех машин владельца
    states_res = await session.execute(
        select(VehicleState, Vehicle.license_plate)
        .join(Vehicle, Vehicle.id == VehicleState.vehicle_id)
        .where(Vehicle.owner_id == owner.id, Vehicle.is_active.is_(True))
    )
    stopped_in_shift: list[tuple] = []   # (vehicle_id, shift_id, driver, plate, since)
    moving_no_shift: list[tuple] = []    # (vehicle_id, plate, speed, since)
    for st, plate in states_res.all():
        if st.last_seen_at is None or st.last_seen_at < stale_cutoff:
            continue  # данные старые — по ним выводов не делаем
        if st.is_valid is False:
            continue
        status = st.motion_status or telemetry_service.vehicle_motion_status(
            st.speed_kmh, st.ignition
        )
        if st.vehicle_id in shift_by_vehicle:
            shift_id, driver_name, shift_plate = shift_by_vehicle[st.vehicle_id]
            long_stop = (
                status == telemetry_service.MOTION_STOPPED
                and st.motion_since_at is not None
                and st.motion_since_at <= stopped_cutoff
            )
            if long_stop:
                stopped_in_shift.append(
                    (st.vehicle_id, shift_id, driver_name, shift_plate, st.motion_since_at)
                )
        elif status == telemetry_service.MOTION_MOVING:
            moving_no_shift.append(
                (st.vehicle_id, plate, Decimal(st.speed_kmh or 0), st.motion_since_at)
            )

    if not moving_no_shift:
        return

    # что уже алёртили за последние MIXUP_DEDUP_HOURS
    recent_res = await session.execute(
        select(Event.event_type, Event.payload).where(
            Event.owner_id == owner.id,
            Event.event_type.in_(("vehicle_mixup_alert", "moving_without_shift_alert")),
            Event.created_at >= dedup_since,
        )
    )
    recent_pairs: set[tuple[int, int]] = set()
    recent_moving: set[int] = set()
    for event_type, payload in recent_res.all():
        payload = payload or {}
        if event_type == "vehicle_mixup_alert":
            recent_pairs.add(
                (payload.get("shift_vehicle_id"), payload.get("moving_vehicle_id"))
            )
        else:
            recent_moving.add(payload.get("vehicle_id"))

    sent_any = False
    if stopped_in_shift:
        # пара «стоит в смене» × «едет без смены» → похоже на перепутанную машину
        for sh_vid, shift_id, driver_name, shift_plate, stop_since in stopped_in_shift:
            for mv_vid, mv_plate, speed, _mv_since in moving_no_shift:
                if (sh_vid, mv_vid) in recent_pairs:
                    continue
                await notify_owner(
                    owner_bot, session, owner,
                    "⚠️ <b>Возможно, перепутана машина.</b>\n"
                    f"<b>{driver_name}</b> в смене на <b>{shift_plate}</b>, но она стоит "
                    f"уже {telemetry_service.duration_label(stop_since, now)}, "
                    f"а <b>{mv_plate}</b> без смены едет ({speed:.0f} км/ч).\n"
                    "Если водитель сел не в ту машину — поменять машину у смены "
                    "можно на сайте, в карточке смены.",
                )
                await log_event(
                    session, owner_id=owner.id, shift_id=shift_id,
                    event_type="vehicle_mixup_alert",
                    payload={"shift_vehicle_id": sh_vid, "moving_vehicle_id": mv_vid},
                )
                recent_pairs.add((sh_vid, mv_vid))
                sent_any = True
    else:
        # никто в смене не «застрял» — тогда это просто движение без смены
        for mv_vid, mv_plate, speed, mv_since in moving_no_shift:
            if mv_vid in recent_moving:
                continue
            await notify_owner(
                owner_bot, session, owner,
                f"🚨 <b>{mv_plate}</b> едет без открытой смены "
                f"({speed:.0f} км/ч, движется {telemetry_service.duration_label(mv_since, now)}).",
            )
            await log_event(
                session, owner_id=owner.id,
                event_type="moving_without_shift_alert",
                payload={"vehicle_id": mv_vid},
            )
            recent_moving.add(mv_vid)
            sent_any = True

    # Напоминание ВОДИТЕЛЮ: его закреплённая машина поехала, а смена не начата.
    if driver_bot is not None:
        sent_any = await _remind_drivers_to_start_shift(
            session, driver_bot, owner, moving_no_shift, active_driver_ids, now
        ) or sent_any

    if sent_any:
        await session.commit()


async def _remind_drivers_to_start_shift(
    session, driver_bot: Bot, owner: Owner,
    moving_no_shift: list[tuple], active_driver_ids: set[int], now: datetime,
) -> bool:
    """Водителю, за кем закреплена машина (default_vehicle_id), которая едет без
    смены, шлём «начните смену». Защита от спама/скачков GPS:
      - машина едет уже ≥ START_REMINDER_MIN_MOVING_MINUTES (не мельком);
      - не чаще раза в START_REMINDER_DEDUP_HOURS на машину (дедуп через events);
      - только если у водителя нет активной смены."""
    from app.bots.notifications import notify_driver

    # оставляем только машины, которые едут достаточно долго (фильтр от скачков GPS)
    steady_vids = telemetry_service.steady_moving_vehicle_ids(
        [(vid, since) for vid, _plate, _speed, since in moving_no_shift],
        now, START_REMINDER_MIN_MOVING_MINUTES,
    )
    if not steady_vids:
        return False

    drivers = (
        await session.execute(
            select(Driver.id, Driver.telegram_id, Driver.default_vehicle_id, Vehicle.license_plate)
            .join(Vehicle, Vehicle.id == Driver.default_vehicle_id)
            .where(
                Driver.owner_id == owner.id,
                Driver.is_active.is_(True),
                Driver.telegram_id.is_not(None),
                Driver.default_vehicle_id.in_(steady_vids),
            )
        )
    ).all()
    if not drivers:
        return False

    reminded_recently = {
        (row[0] or {}).get("vehicle_id")
        for row in (
            await session.execute(
                select(Event.payload).where(
                    Event.owner_id == owner.id,
                    Event.event_type == "start_shift_reminder",
                    Event.created_at >= now - timedelta(hours=START_REMINDER_DEDUP_HOURS),
                )
            )
        ).all()
    }

    sent = False
    for driver_id, telegram_id, vehicle_id, plate in drivers:
        if driver_id in active_driver_ids:
            continue  # уже в смене (возможно, на другой машине)
        if vehicle_id in reminded_recently:
            continue
        await notify_driver(
            driver_bot, session, telegram_id,
            msg.DRIVER_START_SHIFT_REMINDER.format(plate=plate),
        )
        await log_event(
            session, owner_id=owner.id, driver_id=driver_id,
            event_type="start_shift_reminder", payload={"vehicle_id": vehicle_id},
        )
        reminded_recently.add(vehicle_id)
        sent = True
    return sent


# =========================================================================
# Геозоны РЦ — каждые ~5 минут. У РЦ из справочника есть координаты
# (владелец расставляет точки мышкой на /routes). «Приехал» засчитываем,
# только когда машина ОСТАНОВИЛАСЬ в зоне и стоит ≥ RC_MIN_PARKED_MINUTES:
# основные дороги проходят вплотную к РЦ, и проезжающий мимо грузовик
# (или вставший на светофоре) не должен давать ложный «приехал».
# «Уехал» — удалилась дальше выходного радиуса (гистерезис от дрожания GPS).
# Радиус у каждого РЦ может быть свой (geofence_radius_m — большие склады),
# NULL = глобальный RC_GEOFENCE_RADIUS_M. Состояние «внутри/снаружи» — в
# events (rc_arrived / rc_departed), переживает рестарты.
# =========================================================================
RC_GEOFENCE_RADIUS_M = 400
RC_GEOFENCE_EXIT_FACTOR = 1.5   # выходной радиус = входной × 1.5
RC_MIN_PARKED_MINUTES = 4       # столько нужно простоять в зоне для «приехал»
RC_GPS_FRESH_MINUTES = 30
RC_EVENTS_LOOKBACK_DAYS = 30
# Совместимость с тестом гистерезиса и старым кодом
RC_GEOFENCE_EXIT_RADIUS_M = int(RC_GEOFENCE_RADIUS_M * RC_GEOFENCE_EXIT_FACTOR)


def _rc_entry_radius_m(rc) -> int:
    radius = getattr(rc, "geofence_radius_m", None)
    return int(radius) if radius else RC_GEOFENCE_RADIUS_M


def _rc_exit_radius_m(rc) -> int:
    return int(_rc_entry_radius_m(rc) * RC_GEOFENCE_EXIT_FACTOR)




def _event_datetime(value, fallback: datetime) -> datetime:
    if fallback.tzinfo is None:
        fallback = fallback.replace(tzinfo=timezone.utc)
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def rc_geofence_job(owner_bot: Bot) -> None:
    async with async_session() as session:
        owners_res = await session.execute(select(Owner))
        for owner in owners_res.scalars().all():
            try:
                await _check_rc_geofences(session, owner_bot, owner)
            except Exception:
                logger.exception("rc_geofence_job failed for owner %s", owner.id)
                await session.rollback()
                continue


async def _check_rc_geofences(session, owner_bot: Bot, owner: Owner) -> None:
    now = datetime.now(timezone.utc)

    rcs = list(
        (
            await session.execute(
                select(DistributionCenter).where(
                    DistributionCenter.owner_id == owner.id,
                    DistributionCenter.is_active.is_(True),
                    DistributionCenter.latitude.is_not(None),
                    DistributionCenter.longitude.is_not(None),
                )
            )
        ).scalars().all()
    )
    if not rcs:
        return

    fresh_cutoff = now - timedelta(minutes=RC_GPS_FRESH_MINUTES)
    states = [
        (st, plate)
        for st, plate in (
            await session.execute(
                select(VehicleState, Vehicle.license_plate)
                .join(Vehicle, Vehicle.id == VehicleState.vehicle_id)
                .where(Vehicle.owner_id == owner.id, Vehicle.is_active.is_(True))
            )
        ).all()
        if st.last_seen_at is not None
        and st.last_seen_at >= fresh_cutoff
        and st.is_valid is not False
        and st.latitude is not None
        and st.longitude is not None
        # «нулевой остров» — трекер без спутников
        and (abs(float(st.latitude)) > 0.001 or abs(float(st.longitude)) > 0.001)
    ]
    if not states:
        return

    # кто сейчас в смене на машине — для текста уведомления
    shifts_res = await session.execute(
        select(Shift.vehicle_id, Shift.driver_id, Driver.full_name)
        .join(Driver, Driver.id == Shift.driver_id)
        .where(Shift.owner_id == owner.id, Shift.status == "started")
    )
    driver_by_vehicle = {vid: (driver_id, name) for vid, driver_id, name in shifts_res.all()}

    # последнее состояние «внутри/снаружи» по каждой паре (машина, РЦ);
    # для rc_arrived помним и parked_since — когда машина фактически встала.
    # rc_downtime_alert — сигнал при каждом новом 12-часовом блоке платного простоя.
    events_res = await session.execute(
        select(Event.event_type, Event.payload, Event.created_at)
        .where(
            Event.owner_id == owner.id,
            Event.event_type.in_(("rc_arrived", "rc_departed", "rc_downtime_alert")),
            Event.created_at >= now - timedelta(days=RC_EVENTS_LOOKBACK_DAYS),
        )
        .order_by(Event.created_at)
    )
    pair_state: dict[tuple[int, int], dict] = {}
    downtime_alerted: dict[tuple[int, int, str], int] = {}
    for event_type, payload, created_at in events_res.all():
        payload = payload or {}
        vid = telemetry_service.int_or_none(payload.get("vehicle_id"))
        rcid = telemetry_service.int_or_none(payload.get("rc_id"))
        if vid is None or rcid is None:
            continue
        if event_type == "rc_downtime_alert":
            parked_key = str(payload.get("parked_since") or payload.get("arrived_at") or "")
            if parked_key:
                amount = telemetry_service.int_or_none(payload.get("suggested_amount_rub")) or 0
                key = (vid, rcid, parked_key)
                downtime_alerted[key] = max(downtime_alerted.get(key, 0), amount)
            continue
        if event_type in ("rc_arrived", "rc_departed"):
            pair_state[(vid, rcid)] = {
                "type": event_type,
                "at": created_at,
                "parked_since": payload.get("parked_since"),
                "driver_id": payload.get("driver_id"),
                "driver_name": payload.get("driver_name"),
            }

    sent_any = False
    for st, plate in states:
        driver_id, driver_name = driver_by_vehicle.get(st.vehicle_id, (None, None))
        who = f" ({driver_name})" if driver_name else ""
        distances = {
            rc.id: rc_service.haversine_m(
                st.latitude, st.longitude, rc.latitude, rc.longitude
            )
            for rc in rcs
        }

        # 1) Выезды: по всем РЦ, где машина числится «внутри».
        still_inside = False
        for rc in rcs:
            last = pair_state.get((st.vehicle_id, rc.id))
            if last is None or last["type"] != "rc_arrived":
                continue
            if distances[rc.id] >= _rc_exit_radius_m(rc):
                departure_driver_id = telemetry_service.int_or_none(last.get("driver_id")) or driver_id
                departure_driver_name = last.get("driver_name") or driver_name
                departure_who = f" ({departure_driver_name})" if departure_driver_name else ""
                # стоянку считаем с момента фактической остановки (parked_since),
                # а не с момента срабатывания детектора
                arrived_ref = _event_datetime(last.get("parked_since"), last["at"])
                waited_minutes = max(0, int((now - arrived_ref).total_seconds() // 60))
                billable_amount = telemetry_service.rc_billable_downtime_rub(waited_minutes)
                waited = telemetry_service.duration_label(arrived_ref, now)
                # сколько из стоянки двигатель был заглушен (по точкам трекера)
                engine_off = await telemetry_service.engine_off_minutes(
                    session, vehicle_id=st.vehicle_id, start=arrived_ref, end=now
                )
                engine_part = ""
                if engine_off is not None:
                    engine_part = (
                        "\nИз них с заглушенным двигателем: "
                        f"<b>~{telemetry_service.minutes_label(engine_off)}</b>."
                    )
                billable_part = ""
                if billable_amount:
                    billable_part = (
                        "\nПотенциальный простой к выставлению: "
                        f"<b>{telemetry_service.rub_label(billable_amount)}</b> "
                        f"(порог {telemetry_service.minutes_label(telemetry_service.RC_BILLABLE_WAIT_MINUTES)}). "
                        "Автоматически в финансы не добавляю."
                    )
                await notify_owner(
                    owner_bot, session, owner,
                    f"🏁 <b>{plate}</b>{departure_who} уехал с РЦ <b>{rc.name}</b>. "
                    f"Стоял там: <b>{waited}</b>.{engine_part}{billable_part}",
                )
                await log_event(
                    session, owner_id=owner.id, driver_id=departure_driver_id,
                    event_type="rc_departed",
                    payload={
                        "vehicle_id": st.vehicle_id,
                        "rc_id": rc.id,
                        "plate": plate,
                        "rc_name": rc.name,
                        "driver_id": departure_driver_id,
                        "driver_name": departure_driver_name,
                        "arrived_at": arrived_ref.isoformat(),
                        "departed_at": now.isoformat(),
                        "waited_minutes": waited_minutes,
                        "engine_off_minutes": engine_off,
                        "billable_threshold_minutes": telemetry_service.RC_BILLABLE_WAIT_MINUTES,
                        "billable_downtime_rub": billable_amount,
                        "billable_status": (
                            "pending_owner_decision" if billable_amount else "not_billable"
                        ),
                    },
                )
                pair_state[(st.vehicle_id, rc.id)] = {"type": "rc_departed", "at": now}
                sent_any = True
            else:
                still_inside = True
                arrived_ref = _event_datetime(last.get("parked_since"), last["at"])
                waited_minutes = max(0, int((now - arrived_ref).total_seconds() // 60))
                billable_amount = telemetry_service.rc_billable_downtime_rub(waited_minutes)
                parked_key = arrived_ref.isoformat()
                alert_key = (st.vehicle_id, rc.id, parked_key)
                alerted_amount = downtime_alerted.get(alert_key, 0)
                if billable_amount and billable_amount > alerted_amount:
                    alert_driver_id = telemetry_service.int_or_none(last.get("driver_id")) or driver_id
                    alert_driver_name = last.get("driver_name") or driver_name
                    alert_who = f" ({alert_driver_name})" if alert_driver_name else ""
                    await notify_owner(
                        owner_bot, session, owner,
                        f"⏱ <b>{plate}</b>{alert_who} стоит на РЦ <b>{rc.name}</b> уже "
                        f"<b>{telemetry_service.minutes_label(waited_minutes)}</b>.\n"
                        f"Возможный платный простой: "
                        f"<b>{telemetry_service.rub_label(billable_amount)}</b>.\n"
                        "Считаю по 8 000 ₽ за каждые полные 12 часов. "
                        "Пока деньги не добавляю автоматически — проверьте и решите вручную.",
                    )
                    await log_event(
                        session, owner_id=owner.id, driver_id=alert_driver_id,
                        event_type="rc_downtime_alert",
                        payload={
                            "vehicle_id": st.vehicle_id,
                            "rc_id": rc.id,
                            "plate": plate,
                            "rc_name": rc.name,
                            "driver_id": alert_driver_id,
                            "driver_name": alert_driver_name,
                            "parked_since": parked_key,
                            "waited_minutes": waited_minutes,
                            "billable_threshold_minutes": telemetry_service.RC_BILLABLE_WAIT_MINUTES,
                            "suggested_amount_rub": billable_amount,
                            "status": "pending_owner_decision",
                        },
                    )
                    downtime_alerted[alert_key] = billable_amount
                    sent_any = True

        # 2) Въезд. Только если машина ОСТАНОВИЛАСЬ и стоит уже несколько
        # минут: проезжающий мимо по соседней дороге (у нас основные трассы
        # идут вплотную к РЦ) и стоящий на светофоре — не «приехал».
        if still_inside:
            continue
        status = st.motion_status or telemetry_service.vehicle_motion_status(
            st.speed_kmh, st.ignition
        )
        if not telemetry_service.parked_long_enough(
            status, st.motion_since_at, now, RC_MIN_PARKED_MINUTES
        ):
            continue
        # РЦ часто стоят кучно (промзоны): фиксируем только БЛИЖАЙШИЙ РЦ
        # в радиусе — нельзя быть на двух складах сразу.
        candidates = [rc for rc in rcs if distances[rc.id] <= _rc_entry_radius_m(rc)]
        if not candidates:
            continue
        nearest = min(candidates, key=lambda rc: distances[rc.id])
        # Про мотор говорим ТОЛЬКО когда точно знаем (idle_engine = зажигание
        # пришло). Датчик «выкл» в EGTS пока не приходит, поэтому при stopped
        # НЕ утверждаем «заглушен» — это была бы ложь (см. NEXT_SESSION_PROMPT).
        engine_part = (
            ", двигатель работает"
            if status == telemetry_service.MOTION_IDLE_ENGINE
            else ""
        )
        parked_since = st.motion_since_at or now
        since_label = fmt_dt(parked_since, owner.timezone, "%H:%M")
        await notify_owner(
            owner_bot, session, owner,
            f"🏬 <b>{plate}</b>{who} приехал на РЦ <b>{nearest.name}</b> — "
            f"стоит с {since_label}{engine_part}.",
        )
        await log_event(
            session, owner_id=owner.id, driver_id=driver_id, event_type="rc_arrived",
            payload={
                "vehicle_id": st.vehicle_id,
                "rc_id": nearest.id,
                "plate": plate,
                "rc_name": nearest.name,
                "driver_id": driver_id,
                "driver_name": driver_name,
                "parked_since": parked_since.isoformat(),
            },
        )
        pair_state[(st.vehicle_id, nearest.id)] = {
            "type": "rc_arrived", "at": now,
            "parked_since": parked_since.isoformat(),
            "driver_id": driver_id,
            "driver_name": driver_name,
        }
        sent_any = True

        # GPS-сверка ПЛАН ↔ ФАКТ: если у машины есть активный рейс, сверяем
        # РЦ назначения рейса с фактическим РЦ, куда реально приехали.
        active_trip = (
            await session.execute(
                select(Trip)
                .where(
                    Trip.vehicle_id == st.vehicle_id,
                    Trip.owner_id == owner.id,
                    Trip.status.in_(("created", "in_transit", "unloading")),
                )
                .order_by(Trip.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if active_trip is not None:
            # сверяем один раз на рейс
            already_checked = (
                await session.execute(
                    select(Event.id).where(
                        Event.trip_id == active_trip.id,
                        Event.event_type.in_(("trip_rc_confirmed", "trip_rc_mismatch")),
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if already_checked is None:
                planned = rc_service.match_destination_to_center(active_trip.destination, rcs)
                if planned is not None and planned.id == nearest.id:
                    await log_event(
                        session, owner_id=owner.id, driver_id=driver_id,
                        trip_id=active_trip.id, event_type="trip_rc_confirmed",
                        payload={"rc_id": nearest.id, "rc_name": nearest.name},
                    )
                elif planned is not None:
                    await notify_owner(
                        owner_bot, session, owner,
                        f"⚠️ <b>{plate}</b>{who}: по рейсу план — РЦ <b>{planned.name}</b>, "
                        f"а фактически приехал на <b>{nearest.name}</b>. Проверьте.",
                    )
                    await log_event(
                        session, owner_id=owner.id, driver_id=driver_id,
                        trip_id=active_trip.id, event_type="trip_rc_mismatch",
                        payload={
                            "planned_rc_id": planned.id, "planned_rc_name": planned.name,
                            "actual_rc_id": nearest.id, "actual_rc_name": nearest.name,
                        },
                    )
    if sent_any:
        await session.commit()


# =========================================================================
# Чистка телеметрии — раз в сутки. Сырые EGTS-пакеты нужны только для
# отладки/калибровки датчиков, держим 7 дней; разобранные GPS-точки — 60
# дней (для сверки пробега за смену хватает с запасом). Иначе таблицы
# растут бесконечно и зря едят диск на Railway.
# =========================================================================
RAW_PACKETS_KEEP_DAYS = 7
TELEMETRY_POINTS_KEEP_DAYS = 60


async def telemetry_cleanup_job(owner_bot: Bot) -> None:  # noqa: ARG001 — сигнатура как у всех джобов
    from sqlalchemy import delete

    from app.models import VehicleTelemetryPoint, VehicleTelemetryRawPacket

    now = datetime.now(timezone.utc)
    async with async_session() as session:
        try:
            raw_deleted = (
                await session.execute(
                    delete(VehicleTelemetryRawPacket).where(
                        VehicleTelemetryRawPacket.received_at
                        < now - timedelta(days=RAW_PACKETS_KEEP_DAYS)
                    )
                )
            ).rowcount or 0
            points_deleted = (
                await session.execute(
                    delete(VehicleTelemetryPoint).where(
                        VehicleTelemetryPoint.received_at
                        < now - timedelta(days=TELEMETRY_POINTS_KEEP_DAYS)
                    )
                )
            ).rowcount or 0
            await session.commit()
            if raw_deleted or points_deleted:
                logger.info(
                    "telemetry_cleanup: raw>%sd deleted=%s, points>%sd deleted=%s",
                    RAW_PACKETS_KEEP_DAYS, raw_deleted,
                    TELEMETRY_POINTS_KEEP_DAYS, points_deleted,
                )
        except Exception:
            logger.exception("telemetry_cleanup_job failed")
            await session.rollback()
