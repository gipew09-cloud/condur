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
from app.models import DailySummary, Driver, Event, Expense, Owner, Shift, Trip, Vehicle, VehicleState
from app.services import telemetry_service
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
# Еженедельный разбор — воскресенье 20:00 локального времени владельца
# =========================================================================
async def weekly_review_job(owner_bot: Bot) -> None:
    async with async_session() as session:
        owners_res = await session.execute(select(Owner))
        for owner in owners_res.scalars().all():
            try:
                local = now_in_tz(owner.timezone)
                # воскресенье = weekday() == 6, окно 20:00..20:30
                if local.weekday() != 6 or local.hour != 20 or local.minute >= 30:
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
    dedup_since = now - timedelta(hours=24)
    async with async_session() as session:
        drivers_res = await session.execute(
            select(Driver).where(Driver.is_active.is_(True))
        )
        for driver in drivers_res.scalars().all():
            try:
                # есть активная смена? тогда это не невыход
                active = await session.execute(
                    select(Shift.id).where(
                        Shift.driver_id == driver.id, Shift.status == "started"
                    )
                )
                if active.scalar_one_or_none() is not None:
                    continue
                # последняя активность по сменам (или дата подключения водителя)
                last_shift = await session.execute(
                    select(func.max(Shift.started_at)).where(Shift.driver_id == driver.id)
                )
                last_active = last_shift.scalar_one() or driver.created_at
                if last_active is None or last_active > threshold:
                    continue
                # уже алёртили за последние сутки?
                already = await session.execute(
                    select(Event.id).where(
                        Event.driver_id == driver.id,
                        Event.event_type == "no_show_alert",
                        Event.created_at >= dedup_since,
                    )
                )
                if already.scalar_one_or_none() is not None:
                    continue
                owner = await session.get(Owner, driver.owner_id)
                if owner is None:
                    continue
                since_label = last_active.astimezone(
                    owner_tz(owner.timezone)
                ).strftime("%d.%m %H:%M")
                hours = int((now - last_active).total_seconds() // 3600)
                await notify_owner(
                    owner_bot, session, owner,
                    f"🚷 <b>{driver.full_name}</b> не выходил на смену с {since_label} "
                    f"(~{hours} ч). Возможно, простаивает — есть кому дать работу?",
                )
                await log_event(
                    session, owner_id=owner.id, driver_id=driver.id,
                    event_type="no_show_alert", payload={"hours": hours},
                )
                await session.commit()
            except Exception:
                logger.exception("no_show_detector_job failed for driver %s", driver.id)
                await session.rollback()
                continue
