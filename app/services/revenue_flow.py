"""Выручка рейса глазами владельца: указать самому или утвердить сумму водителя.

Владелец 28.09.2026: «кнопки „Указать выручку“ нету» — в приложении её не
было, выручку можно было вписать только в Telegram (и на сайте на странице
рейса). Теперь одна логика на приложение и сайт, с теми же последствиями,
что в боте: сумма, запись в журнал, погашенные кнопки в Telegram.

⚠️ Кнопки в Telegram гасим ПОСЛЕ записи: иначе у водителя осталась бы
«Указать выручку», а у владельца — «Одобрить/Изменить» с устаревшей суммой, и
вторым нажатием можно было бы перезаписать уже закрытое число.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Owner, Trip
from app.services import trip_service
from app.services.event_service import log_event

logger = logging.getLogger(__name__)

# Больше не влезает в колонку выручки (Numeric(12, 2)).
REVENUE_MAX = Decimal("9999999999.99")


def action_for(event_type: str, trip: Trip | None) -> dict | None:
    """Что владелец может сделать с выручкой прямо из журнала.

    Кнопка стоит на ОДНОЙ карточке рейса, а не на всех: сумма водителя ждёт
    решения — на «Выручку назвал водитель»; выручки нет совсем — на «Груз
    сдан» (или «Рейс добавлен вручную»). Выручка есть — кнопки нет.
    """
    if trip is None or trip.status != "completed" or trip.revenue_rub is not None:
        return None
    pending = trip.driver_revenue_pending_rub
    if pending is not None:
        if event_type != "trip_revenue_from_driver":
            return None
        return {"trip_id": trip.id, "pending": float(pending)}
    if event_type not in ("trip_completed", "trip_added_manual"):
        return None
    return {"trip_id": trip.id, "pending": None}


async def _drop_telegram_buttons(session: AsyncSession, trip_id: int, driver_bot, owner_bot) -> None:
    from app.bots.notifications import (
        drop_revenue_decision_buttons,
        drop_revenue_prompt_buttons,
    )

    try:
        if driver_bot is not None:
            await drop_revenue_prompt_buttons(session, trip_id, driver_bot=driver_bot, side="driver")
        if owner_bot is not None:
            await drop_revenue_decision_buttons(session, trip_id, owner_bot=owner_bot)
    except Exception as exc:  # noqa: BLE001 — сумма уже записана, кнопки — вторично
        logger.warning("Кнопки выручки в Telegram не погашены (trip=%s): %s", trip_id, exc)


async def owner_sets(
    session: AsyncSession, *, owner: Owner, trip: Trip, revenue: Decimal,
    driver_bot=None, owner_bot=None,
) -> None:
    """Владелец вписал выручку сам (или исправил сумму водителя)."""
    await trip_service.set_trip_revenue(session, trip=trip, revenue_rub=revenue)
    await log_event(
        session, owner_id=owner.id, driver_id=trip.driver_id,
        shift_id=trip.shift_id, trip_id=trip.id,
        event_type="trip_revenue_set", payload={"revenue": str(trip.revenue_rub)},
    )
    await session.commit()
    await _drop_telegram_buttons(session, trip.id, driver_bot, owner_bot)


async def owner_approves(
    session: AsyncSession, *, owner: Owner, trip: Trip, driver_bot=None, owner_bot=None,
) -> bool:
    """Владелец согласился с суммой водителя. `False` — утверждать нечего
    (сумму уже закрыли, например из Telegram): повтор нажатия ничего не ломает."""
    if not await trip_service.approve_trip_driver_revenue(session, trip=trip):
        return False
    await log_event(
        session, owner_id=owner.id, driver_id=trip.driver_id,
        shift_id=trip.shift_id, trip_id=trip.id,
        event_type="trip_revenue_approved", payload={"revenue": str(trip.revenue_rub)},
    )
    await session.commit()
    await _drop_telegram_buttons(session, trip.id, driver_bot, owner_bot)
    return True
