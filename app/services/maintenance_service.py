"""
Полный сброс тестовых данных владельца (скрытая команда /wipe в боте владельца).

Удаляет ВСЕ рабочие данные кабинета: рейсы, смены, водителей, машины, расходы,
документы, GPS-телеметрию, справочники. НЕ трогает сам аккаунт владельца
(вход в бот и кабинет остаётся), его реквизиты Исполнителя, админов,
веб-сессии (устройства) и тариф.

Команды нет в /help — она только для перехода на новую стадию теста.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from app.models import (
    Customer,
    DailySummary,
    DistributionCenter,
    Driver,
    Event,
    Expense,
    ManualEntry,
    RouteTemplate,
    Shift,
    Trip,
    TripDocument,
    Vehicle,
    VehicleState,
    VehicleTelemetryPoint,
    VehicleTelemetryRawPacket,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Точная фраза подтверждения — защита от случайного нажатия.
WIPE_CONFIRM_PHRASE = "УДАЛИТЬ ВСЁ"


async def wipe_owner_data(session: AsyncSession, owner_id: int) -> dict[str, int]:
    """Стереть все данные владельца. Возвращает счётчики по разделам.

    Порядок удаления учитывает связи между таблицами. Коммит — на вызывающей
    стороне (бот), чтобы всё прошло одной транзакцией.
    """
    vehicle_ids = [
        row[0] for row in (
            await session.execute(select(Vehicle.id).where(Vehicle.owner_id == owner_id))
        ).all()
    ]

    async def _del(stmt) -> int:
        return (await session.execute(stmt)).rowcount or 0

    counts: dict[str, int] = {}
    counts["события"] = await _del(delete(Event).where(Event.owner_id == owner_id))
    counts["расходы"] = await _del(delete(Expense).where(Expense.owner_id == owner_id))
    counts["документы рейсов"] = await _del(
        delete(TripDocument).where(TripDocument.owner_id == owner_id)
    )
    counts["GPS-точки"] = await _del(
        delete(VehicleTelemetryPoint).where(VehicleTelemetryPoint.owner_id == owner_id)
    )
    if vehicle_ids:
        counts["GPS-состояния"] = await _del(
            delete(VehicleState).where(VehicleState.vehicle_id.in_(vehicle_ids))
        )
        counts["GPS-пакеты"] = await _del(
            delete(VehicleTelemetryRawPacket).where(
                VehicleTelemetryRawPacket.vehicle_id.in_(vehicle_ids)
            )
        )
    counts["рейсы"] = await _del(delete(Trip).where(Trip.owner_id == owner_id))
    counts["смены"] = await _del(delete(Shift).where(Shift.owner_id == owner_id))
    counts["водители"] = await _del(delete(Driver).where(Driver.owner_id == owner_id))
    counts["машины"] = await _del(delete(Vehicle).where(Vehicle.owner_id == owner_id))
    counts["шаблоны маршрутов"] = await _del(
        delete(RouteTemplate).where(RouteTemplate.owner_id == owner_id)
    )
    counts["ручные записи"] = await _del(
        delete(ManualEntry).where(ManualEntry.owner_id == owner_id)
    )
    counts["дневные сводки"] = await _del(
        delete(DailySummary).where(DailySummary.owner_id == owner_id)
    )
    counts["справочник РЦ"] = await _del(
        delete(DistributionCenter).where(DistributionCenter.owner_id == owner_id)
    )
    counts["заказчики"] = await _del(delete(Customer).where(Customer.owner_id == owner_id))
    return {k: v for k, v in counts.items() if v}
