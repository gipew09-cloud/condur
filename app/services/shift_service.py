"""
Бизнес-логика смен.

Принцип: сервис не знает про Telegram. Он принимает session и параметры,
возвращает объекты. Хендлер бота сам формирует ответы.
"""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Shift, Trip, Vehicle


async def get_active_shift(session: AsyncSession, driver_id: int) -> Shift | None:
    """Активная смена водителя (статус 'started'). None — если такой нет.

    Открытая смена должна быть одна, но если в базе оказались две (старые
    дубликаты от двойного нажатия), берём самую свежую: падать на каждом
    действии водителя — хуже, чем взять последнюю.
    """
    result = await session.execute(
        select(Shift)
        .where(Shift.driver_id == driver_id, Shift.status == "started")
        .order_by(Shift.id.desc())
        .limit(1)
    )
    return result.scalars().first()


async def get_free_vehicles(session: AsyncSession, owner_id: int) -> list[Vehicle]:
    """Машины владельца, которые сейчас не заняты другой сменой."""
    busy_q = select(Shift.vehicle_id).where(
        Shift.owner_id == owner_id, Shift.status == "started"
    )
    result = await session.execute(
        select(Vehicle)
        .where(
            Vehicle.owner_id == owner_id,
            Vehicle.is_active.is_(True),
            Vehicle.id.notin_(busy_q),
        )
        .order_by(Vehicle.license_plate)
    )
    return list(result.scalars().all())


async def start_shift(
    session: AsyncSession,
    *,
    owner_id: int,
    driver_id: int,
    vehicle_id: int,
    odometer_start: int,
    photo_file_id: str | None,
) -> Shift:
    """Создаёт смену со статусом 'started'. Коммит делает вызывающий."""
    shift = Shift(
        owner_id=owner_id,
        driver_id=driver_id,
        vehicle_id=vehicle_id,
        odometer_start=odometer_start,
        odometer_start_photo_url=photo_file_id,
        status="started",
    )
    session.add(shift)
    return shift


async def end_shift(
    session: AsyncSession,
    *,
    shift: Shift,
    odometer_end: int,
    photo_file_id: str | None,
    ended_at,
) -> Shift:
    """Закрывает смену. Подсчёт distance_km делает Postgres (Computed column)."""
    shift.odometer_end = odometer_end
    shift.odometer_end_photo_url = photo_file_id
    shift.ended_at = ended_at
    shift.status = "completed"
    return shift


async def get_shift_trips(session: AsyncSession, shift_id: int) -> list[Trip]:
    result = await session.execute(
        select(Trip).where(Trip.shift_id == shift_id).order_by(Trip.created_at)
    )
    return list(result.scalars().all())


async def get_shift_revenue(session: AsyncSession, shift_id: int) -> Decimal:
    trips = await get_shift_trips(session, shift_id)
    return sum((t.revenue_rub or Decimal(0)) for t in trips) or Decimal(0)
