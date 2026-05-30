"""
Бизнес-логика рейсов.

Открытый рейс — это Trip со статусом, не равным 'completed' и 'cancelled'.
В одной смене может быть только один открытый рейс.
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Shift, Trip

# средняя цена литра 92-го по РФ на момент MVP, используется,
# если водитель не уточнил собственную цену
DEFAULT_FUEL_PRICE_RUB_PER_LITER = Decimal("68")


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
    revenue_rub: Decimal,
    fuel_liters: Decimal,
    fuel_price: Decimal = DEFAULT_FUEL_PRICE_RUB_PER_LITER,
) -> Trip:
    trip.revenue_rub = revenue_rub
    trip.fuel_cost_rub = (fuel_liters * fuel_price).quantize(Decimal("0.01"))
    trip.status = "completed"
    trip.completed_at = datetime.now(timezone.utc)
    # profit_rub считает Postgres через Computed column после flush/commit
    return trip
