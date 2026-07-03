"""
Бизнес-логика рейсов.

Открытый рейс — это Trip со статусом, не равным 'completed' и 'cancelled'.
В одной смене может быть только один открытый рейс.
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Expense, Shift, Trip

# средняя цена литра 92-го по РФ на момент MVP, используется,
# если водитель не уточнил собственную цену
DEFAULT_FUEL_PRICE_RUB_PER_LITER = Decimal("68")


def liters_from_rub(amount_rub: Decimal) -> Decimal:
    return (amount_rub / DEFAULT_FUEL_PRICE_RUB_PER_LITER).quantize(Decimal("0.1"))


async def get_active_trip(session: AsyncSession, shift_id: int) -> Trip | None:
    """Открытый рейс в смене (created / in_transit / unloading)."""
    result = await session.execute(
        select(Trip).where(
            Trip.shift_id == shift_id,
            Trip.status.in_(("created", "in_transit", "unloading")),
        )
    )
    return result.scalar_one_or_none()


async def create_trip(
    session: AsyncSession,
    *,
    shift: Shift,
    origin: str,
    destination: str,
    cargo_name: str,
) -> Trip:
    trip = Trip(
        owner_id=shift.owner_id,
        shift_id=shift.id,
        driver_id=shift.driver_id,
        vehicle_id=shift.vehicle_id,
        status="created",
        origin=origin,
        destination=destination,
        cargo_name=cargo_name,
    )
    session.add(trip)
    return trip


async def set_trip_status(
    session: AsyncSession, *, trip: Trip, status: str
) -> Trip:
    if status not in ("in_transit", "unloading"):
        raise ValueError(f"Недопустимый статус для перехода: {status}")
    trip.status = status
    return trip


async def attach_waybill(
    session: AsyncSession, *, trip: Trip, photo_file_id: str
) -> Trip:
    trip.waybill_photo_url = photo_file_id
    return trip


async def complete_trip(
    session: AsyncSession,
    *,
    trip: Trip,
) -> Trip:
    """
    Завершаем рейс без вопроса литров/выручки.
    fuel_cost_rub считается из расходов категории 'fuel', привязанных
    к этому рейсу (включая pending — потому что водитель может ещё ждать
    одобрения, а P&L уже хочется видеть). Выручку владелец укажет позже
    отдельным callback'ом.
    """
    fuel_total = await session.execute(
        select(Expense.amount_rub).where(
            Expense.trip_id == trip.id, Expense.category == "fuel"
        )
    )
    fuel_sum = sum((row[0] or Decimal(0)) for row in fuel_total.all()) or Decimal(0)
    trip.fuel_cost_rub = Decimal(fuel_sum).quantize(Decimal("0.01"))

    # Прочие расходы рейса (не топливо, не отклонённые) → other_costs_rub,
    # чтобы прибыль = выручка − топливо − прочее была осмысленной (Блок G2).
    other_total = await session.execute(
        select(Expense.amount_rub).where(
            Expense.trip_id == trip.id,
            Expense.category != "fuel",
            Expense.status != "rejected",
        )
    )
    other_sum = sum((row[0] or Decimal(0)) for row in other_total.all()) or Decimal(0)
    trip.other_costs_rub = Decimal(other_sum).quantize(Decimal("0.01"))

    trip.status = "completed"
    trip.completed_at = datetime.now(timezone.utc)
    return trip


async def set_trip_revenue(
    session: AsyncSession, *, trip: Trip, revenue_rub: Decimal
) -> Trip:
    trip.revenue_rub = revenue_rub.quantize(Decimal("0.01"))
    return trip


async def set_trip_revenue_if_empty(
    session: AsyncSession, *, trip: Trip, revenue_rub: Decimal
) -> bool:
    """
    «Правило первого» для ВОДИТЕЛЯ, устойчивое к гонке: атомарный UPDATE
    записывает выручку только если её ещё нет. Если владелец успел указать
    свою (пока водитель печатал число) — вернём False и ничего не перетрём.
    Владелец пишет через set_trip_revenue — он главный и перетирает всегда.
    """
    result = await session.execute(
        update(Trip)
        .where(Trip.id == trip.id, Trip.revenue_rub.is_(None))
        .values(revenue_rub=revenue_rub.quantize(Decimal("0.01")))
    )
    await session.flush()
    # Обновляем объект в сессии, чтобы дальше показывать актуальное значение.
    await session.refresh(trip)
    return result.rowcount == 1
