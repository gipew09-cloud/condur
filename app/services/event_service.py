"""
Лог событий. Любое значимое действие водителя пишем сюда.

Используется для:
  - аудита ("что делал водитель X сегодня"),
  - детектора тишины ("последнее событие — N часов назад"),
  - формирования дневной сводки.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event


async def log_event(
    session: AsyncSession,
    *,
    owner_id: int,
    event_type: str,
    driver_id: int | None = None,
    shift_id: int | None = None,
    trip_id: int | None = None,
    payload: dict | None = None,
) -> Event:
    """Создать запись Event. Коммит на вызывающей стороне."""
    event = Event(
        owner_id=owner_id,
        driver_id=driver_id,
        shift_id=shift_id,
        trip_id=trip_id,
        event_type=event_type,
        payload=payload,
    )
    session.add(event)
    return event
