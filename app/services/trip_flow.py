"""
Рейс — ОДНА логика для бота и приложения (как `shift_flow.py` для смены).

Здесь всё, что относится к самому рейсу: запись в базу, событие в журнал и
текст уведомления владельцу. Как отвечать водителю — решает тот, кто вызвал.
Коммит делает вызывающий.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import keyboards as kb
from app.bots import messages as msg
from app.config import settings
from app.models import Driver, RouteTemplate, Shift, Trip
from app.services import trip_service
from app.services.event_service import log_event
from app.services.textsanitize import origin_key


async def route_catalog(session: AsyncSession, owner_id: int) -> list[dict]:
    """Маршруты владельца папками по складам — для телефона.

    ⚠️ Порядок и склейка складов те же, что в боте (`_route_origins`,
    `_templates_for_origin`): склады по алфавиту ключа, внутри — как
    расставил владелец на сайте. Закреплено тестом.
    """
    templates = (await session.execute(
        select(RouteTemplate)
        .where(RouteTemplate.owner_id == owner_id, RouteTemplate.is_active.is_(True))
        .order_by(RouteTemplate.sort_order, RouteTemplate.destination, RouteTemplate.name)
    )).scalars().all()
    folders: dict[str, list[dict]] = {}
    for template in templates:
        key = origin_key(template.origin)
        if not key:
            continue
        folders.setdefault(key, []).append({
            "id": template.id,
            "destination": template.destination,
            "cargo": template.default_cargo,
        })
    return [{"origin": key, "routes": folders[key]} for key in sorted(folders)]


def can_finish(status: str) -> bool:
    """Из каких статусов разрешено «Сдал груз». При выключенных промежуточных
    статусах (FEATURE_TRIP_STATUS_STEPS) рейс завершается прямо из in_transit."""
    if settings.feature_trip_status_steps:
        return status == "unloading"
    return status in ("in_transit", "unloading")


async def _location_event(session, driver, shift, trip, location, context):
    if location is None:
        return
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="location_sent",
        payload={"lat": location[0], "lon": location[1], "context": context},
    )


async def create_trip(
    session: AsyncSession,
    *,
    driver: Driver,
    shift: Shift,
    origin: str,
    destination: str,
    cargo: str | None,
    source: str,
    now: datetime | None = None,
) -> Trip:
    trip = await trip_service.create_trip(
        session, shift=shift, origin=origin, destination=destination, cargo_name=cargo,
    )
    if now is not None:
        trip.created_at = now
    await session.flush()
    await log_event(
        session,
        owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_created",
        payload={"origin": origin, "destination": destination, "source": source},
    )
    return trip


def created_owner_text(trip: Trip, *, driver: Driver) -> str:
    return msg.NOTIFY_TRIP_CREATED.format(
        driver=driver.full_name, origin=trip.origin, destination=trip.destination,
        cargo=trip.cargo_name or "—",
    )


async def depart(
    session: AsyncSession,
    *,
    driver: Driver,
    shift: Shift,
    trip: Trip,
    source: str,
    location: tuple[float, float] | None = None,
) -> None:
    await trip_service.set_trip_status(session, trip=trip, status="in_transit")
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_in_transit",
        payload={"source": source},
    )
    await _location_event(session, driver, shift, trip, location, "depart")


def departed_owner_text(trip: Trip, *, driver: Driver) -> str:
    return msg.NOTIFY_TRIP_IN_TRANSIT.format(
        driver=driver.full_name, origin=trip.origin or "—",
        destination=trip.destination or "—",
    )


async def start_unloading(
    session: AsyncSession,
    *,
    driver: Driver,
    shift: Shift,
    trip: Trip,
    source: str,
    location: tuple[float, float] | None = None,
) -> None:
    await trip_service.set_trip_status(session, trip=trip, status="unloading")
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_unloading",
        payload={"source": source},
    )
    await _location_event(session, driver, shift, trip, location, "unloading")


def unloading_owner_text(trip: Trip, *, driver: Driver) -> str:
    return msg.NOTIFY_TRIP_UNLOADING.format(
        driver=driver.full_name, destination=trip.destination or "—",
    )


async def finish(
    session: AsyncSession,
    *,
    driver: Driver,
    shift: Shift,
    trip: Trip,
    source: str,
    location: tuple[float, float] | None = None,
    now: datetime | None = None,
) -> Decimal:
    """Завершить рейс. Возвращает топливо по рейсу, ₽."""
    await trip_service.complete_trip(session, trip=trip)
    if now is not None:
        trip.completed_at = now
    await session.flush()
    await session.refresh(trip)
    fuel = Decimal(trip.fuel_cost_rub or 0)
    # Порядок событий как был в боте: геопозиция, потом «рейс завершён».
    await _location_event(session, driver, shift, trip, location, "trip_end")
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_completed",
        payload={"fuel_cost": str(fuel), "source": source},
    )
    return fuel


def completed_owner_text(trip: Trip, *, driver: Driver, fuel: Decimal) -> str:
    return msg.NOTIFY_TRIP_COMPLETED.format(
        driver=driver.full_name,
        origin=trip.origin or "—", destination=trip.destination or "—",
        fuel=f"{fuel:.0f}",
    )


def revenue_markup(trip: Trip):
    """Кнопка владельцу «Указать выручку» под завершённым рейсом."""
    return kb.trip_revenue_keyboard(trip.id)


async def remember_revenue_prompt(
    session: AsyncSession,
    *,
    driver: Driver,
    trip: Trip,
    owner_chat_id: int | None,
    owner_msg_id: int | None,
    driver_chat_id: int | None = None,
    driver_msg_id: int | None = None,
) -> None:
    """Запомнить сообщения с кнопками выручки: когда одна сторона укажет
    сумму, у другой кнопка погаснет (`drop_revenue_prompt_buttons`)."""
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=trip.shift_id, trip_id=trip.id, event_type="trip_revenue_prompt",
        payload={
            "driver_chat_id": driver_chat_id,
            "driver_msg_id": driver_msg_id,
            "owner_chat_id": owner_chat_id,
            "owner_msg_id": owner_msg_id,
        },
    )
