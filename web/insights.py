"""
Автоматические замечания для дашборда.

Все проверки идут на данных владельца за последние 30 дней.
Каждая функция возвращает list[str] — строки в формате
«[тип] — текст замечания». Тип используем эмодзи для визуального
различения важности.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Driver, Shift, Trip, Vehicle

WINDOW_DAYS = 30
FUEL_OVERUSE_THRESHOLD = Decimal("1.10")  # >10% выше нормы
IDLE_VEHICLE_DAYS = 7


async def _fuel_overuse(session: AsyncSession, owner_id: int, since) -> list[str]:
    """
    Сравниваем фактический расход (литры/100км) с нормой по машине.
    Фактический литр = fuel_cost / 68. distance по рейсу — пропорционально
    смене (упрощение: дистанция рейса = distance_km смены / число рейсов смены).
    Для MVP считаем агрегированно: сумма литров vs сумма км по машине.
    """
    # сумма литров (приведённая из fuel_cost) и сумма км по машине за окно
    result = await session.execute(
        select(
            Vehicle.id,
            Vehicle.license_plate,
            Vehicle.fuel_norm_per_100km,
            func.coalesce(func.sum(Trip.fuel_cost_rub), 0).label("fuel_cost"),
            func.coalesce(func.sum(Shift.distance_km), 0).label("distance"),
        )
        .join(Shift, Shift.vehicle_id == Vehicle.id)
        .join(Trip, Trip.shift_id == Shift.id)
        .where(
            Vehicle.owner_id == owner_id,
            Trip.status == "completed",
            Trip.completed_at >= since,
            Vehicle.fuel_norm_per_100km.is_not(None),
        )
        .group_by(Vehicle.id, Vehicle.license_plate, Vehicle.fuel_norm_per_100km)
    )
    insights = []
    for vid, plate, norm, fuel_cost, distance in result.all():
        if not distance or not norm:
            continue
        liters = Decimal(fuel_cost) / Decimal("68")
        actual_per_100 = (liters / Decimal(distance)) * Decimal("100")
        if actual_per_100 > Decimal(norm) * FUEL_OVERUSE_THRESHOLD:
            pct = ((actual_per_100 / Decimal(norm)) - Decimal(1)) * Decimal(100)
            insights.append(
                f"⛽️ Машина <b>{plate}</b> расходует топлива на "
                f"<b>{pct:.0f}%</b> больше нормы ({actual_per_100:.1f} vs {norm} л/100км)."
            )
    return insights


async def _unprofitable_trips(session: AsyncSession, owner_id: int, since) -> list[str]:
    result = await session.execute(
        select(func.count(Trip.id))
        .where(
            Trip.owner_id == owner_id,
            Trip.status == "completed",
            Trip.completed_at >= since,
            Trip.profit_rub < 0,
        )
    )
    count = result.scalar_one() or 0
    if count == 0:
        return []
    return [
        f"🔴 <b>{count}</b> {_plural(count, 'рейс', 'рейса', 'рейсов')} "
        f"за последние {WINDOW_DAYS} дней оказались убыточными."
    ]


async def _idle_vehicles(session: AsyncSession, owner_id: int) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=IDLE_VEHICLE_DAYS)
    last_trip_subq = (
        select(Trip.vehicle_id, func.max(Trip.completed_at).label("last_at"))
        .where(Trip.owner_id == owner_id, Trip.status == "completed")
        .group_by(Trip.vehicle_id)
        .subquery()
    )
    result = await session.execute(
        select(Vehicle.license_plate, last_trip_subq.c.last_at)
        .outerjoin(last_trip_subq, last_trip_subq.c.vehicle_id == Vehicle.id)
        .where(Vehicle.owner_id == owner_id, Vehicle.is_active.is_(True))
    )
    insights = []
    for plate, last_at in result.all():
        if last_at is None:
            insights.append(f"⏸ Машина <b>{plate}</b> ещё ни разу не выезжала.")
        elif last_at < cutoff:
            days = (datetime.now(timezone.utc) - last_at).days
            insights.append(f"⏸ Машина <b>{plate}</b> простаивает {days} дней.")
    return insights


async def _low_revenue_per_km(session: AsyncSession, owner_id: int, since) -> list[str]:
    """Если у водителя <10 ₽ выручки за км пробега — флажок."""
    result = await session.execute(
        select(
            Driver.id,
            Driver.full_name,
            func.coalesce(func.sum(Trip.revenue_rub), 0).label("revenue"),
            func.coalesce(func.sum(Shift.distance_km), 0).label("distance"),
        )
        .join(Shift, Shift.driver_id == Driver.id)
        .outerjoin(Trip, (Trip.shift_id == Shift.id) & (Trip.status == "completed"))
        .where(
            Driver.owner_id == owner_id,
            Shift.status == "completed",
            Shift.ended_at >= since,
        )
        .group_by(Driver.id, Driver.full_name)
    )
    insights = []
    for _id, name, revenue, distance in result.all():
        if not distance or distance < 100:
            continue
        per_km = Decimal(revenue) / Decimal(distance)
        if per_km < Decimal("10"):
            insights.append(
                f"📉 Водитель <b>{name}</b> приносит "
                f"<b>{per_km:.1f} ₽/км</b> — это мало."
            )
    return insights


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


async def generate_insights(session: AsyncSession, owner_id: int) -> list[str]:
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    insights: list[str] = []
    insights.extend(await _fuel_overuse(session, owner_id, since))
    insights.extend(await _unprofitable_trips(session, owner_id, since))
    insights.extend(await _idle_vehicles(session, owner_id))
    insights.extend(await _low_revenue_per_km(session, owner_id, since))
    return insights
