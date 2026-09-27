"""
Рейс — ОДНА логика для бота и приложения (как `shift_flow.py` для смены).

Здесь всё, что относится к самому рейсу: запись в базу, событие в журнал и
текст уведомления владельцу. Как отвечать водителю — решает тот, кто вызвал.
Коммит делает вызывающий.
"""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import keyboards as kb
from app.bots import messages as msg
from app.config import settings
from app.models import Driver, RouteTemplate, Shift, Trip, Vehicle
from app.services import route_order, trip_service
from app.services.event_service import log_event
from app.services.textsanitize import clean_user_text
from app.services.timeutil import owner_tz


async def route_catalog(session: AsyncSession, owner_id: int) -> list[dict]:
    """Маршруты владельца папками по складам — для телефона.

    ⚠️ Порядок и склейка складов те же, что в боте (`_route_origins`,
    `_templates_for_origin`) и на сайте: `route_order.grouped` — склады и
    маршруты как расставил владелец, пока склады не переставлены — по
    алфавиту ключа. Закреплено тестом.
    """
    templates = (await session.execute(
        select(RouteTemplate)
        .where(RouteTemplate.owner_id == owner_id, RouteTemplate.is_active.is_(True))
        .order_by(RouteTemplate.sort_order, RouteTemplate.destination, RouteTemplate.name)
    )).scalars().all()
    return [
        {
            "origin": key,
            "routes": [
                {"id": t.id, "destination": t.destination, "cargo": t.default_cargo}
                for t in routes
            ],
        }
        for key, routes in route_order.grouped(templates).items()
    ]


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


def _h(value: str | None) -> str:
    """Текст для сообщения в Telegram (разметка HTML).

    «<» и «>» из слов водителя убирает `clean_user_text`, а «&» по правилам
    разметки Telegram тоже положено заменять: иначе, например, «&lt;b&gt;»,
    написанное водителем в своём маршруте, Telegram показал бы как «<b>»
    (проверка 26.09.2026). Имена и названия от владельца — туда же.
    """
    return html.escape(value or "—", quote=False)


# ------------------------------------------------------------ свой маршрут
# Откуда/куда/груз, которые водитель написал сам, а не выбрал из списка
# (владелец 26.09.2026: «чтобы он смог сам писать откуда он там что делает»).
PLACE_MAX = 120


def clean_place(value) -> str:
    """Текст водителя для рейса: без HTML, пробелы схлопнуты, не длиннее
    120 знаков. Не строка — пусто."""
    if not isinstance(value, str):
        return ""
    return " ".join(clean_user_text(value).split())[:PLACE_MAX]


# -------------------------------------------------------- рейс задним числом
# Водитель забыл отметить рейс — добавляет его потом, без GPS и одометра.
# Общее для бота («➕ Добавить рейс») и приложения.

def manual_moment(day: date, tz_name: str | None) -> datetime | None:
    """Полдень этого дня по часам владельца, в UTC. Будущий день — None:
    рейс, который ещё не случился, задним числом не добавляют."""
    tz = owner_tz(tz_name)
    if day > datetime.now(tz).date():
        return None
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=tz).astimezone(timezone.utc)


# Сколько рейсов задним числом водитель может добавить за сутки. Каждый
# такой рейс — уведомление владельцу; без предела телефоном (или скриптом с
# его входом) можно было бы засыпать владельца сообщениями. Больше двадцати
# «забытых» рейсов за сутки не бывает — это уже разбор, его ведёт владелец.
MANUAL_PER_DAY = 20
MANUAL_TOO_MANY = (
    "За сутки добавлено слишком много рейсов задним числом. "
    "Остальные пусть добавит владелец на сайте."
)


async def manual_limit_reached(session: AsyncSession, driver_id: int) -> bool:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    count = (await session.execute(
        select(func.count(Trip.id)).where(
            Trip.driver_id == driver_id,
            Trip.is_manual.is_(True),
            Trip.created_at >= since,
        )
    )).scalar_one()
    return count >= MANUAL_PER_DAY


async def add_manual_trip(
    session: AsyncSession,
    *,
    driver: Driver,
    vehicle: Vehicle,
    origin: str,
    destination: str,
    cargo: str | None,
    when: datetime,
    source: str,
) -> Trip:
    """Рейс задним числом. Живёт в своей ручной (завершённой) смене — чтобы
    у рейса была смена, как у всех. Пробег у такого рейса неизвестен."""
    shift = Shift(
        owner_id=driver.owner_id, driver_id=driver.id, vehicle_id=vehicle.id,
        status="completed", started_at=when, ended_at=when, is_manual=True,
    )
    session.add(shift)
    await session.flush()
    trip = await trip_service.create_trip(
        session, shift=shift, origin=origin, destination=destination, cargo_name=cargo,
    )
    await session.flush()
    trip.status = "completed"
    trip.completed_at = when
    trip.is_manual = True
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_added_manual",
        payload={
            "origin": origin, "destination": destination,
            "date": when.isoformat(), "source": source,
        },
    )
    return trip


def manual_date_label(when: datetime, tz_name: str | None) -> str:
    return when.astimezone(owner_tz(tz_name)).strftime("%d.%m.%Y")


def manual_owner_text(trip: Trip, *, driver: Driver, plate: str, tz_name: str | None) -> str:
    return msg.NOTIFY_MANUAL_TRIP.format(
        driver=_h(driver.full_name), date=manual_date_label(trip.completed_at, tz_name),
        origin=_h(trip.origin), destination=_h(trip.destination), plate=_h(plate),
    )


def created_owner_text(trip: Trip, *, driver: Driver) -> str:
    return msg.NOTIFY_TRIP_CREATED.format(
        driver=_h(driver.full_name), origin=_h(trip.origin),
        destination=_h(trip.destination), cargo=_h(trip.cargo_name),
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
        driver=_h(driver.full_name), origin=_h(trip.origin),
        destination=_h(trip.destination),
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
        driver=_h(driver.full_name), destination=_h(trip.destination),
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
        driver=_h(driver.full_name),
        origin=_h(trip.origin), destination=_h(trip.destination),
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
