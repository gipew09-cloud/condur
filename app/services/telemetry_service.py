"""
Выжимки из GPS-телеметрии для бота и кабинета.

Пробег за период считаем по mileage_km (одометр самого трекера Stavtrack:
max − min за период), а НЕ суммой расстояний между координатами — счётчик
прибора не «прыгает», когда GPS лагает в городе, поэтому сравнение с
одометром машины честное.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Больше этой доли расхождение одометра и GPS считаем подозрительным.
MILEAGE_MISMATCH_ALERT_RATIO = Decimal("0.10")

MOTION_MOVING = "moving"
MOTION_IDLE_ENGINE = "idle_engine"
MOTION_STOPPED = "stopped"
MOTION_UNKNOWN = "unknown"


def vehicle_motion_status(speed_kmh: Decimal | float | int | None, ignition: bool | None) -> str:
    """Текущий статус машины по GPS/Stavtrack."""
    speed = Decimal(str(speed_kmh or 0))
    if speed > Decimal("3"):
        return MOTION_MOVING
    if ignition:
        return MOTION_IDLE_ENGINE
    return MOTION_STOPPED


def motion_status_text(status: str | None, speed_kmh: Decimal | float | int | None = None) -> str:
    speed = Decimal(str(speed_kmh or 0))
    if status == MOTION_MOVING:
        return f"едет · {speed:.0f} км/ч"
    if status == MOTION_IDLE_ENGINE:
        return "стоит, двигатель работает"
    if status == MOTION_STOPPED:
        return "стоит"
    return "нет данных"


def duration_label(start: datetime | None, end: datetime | None = None) -> str:
    """Короткая длительность: 8 мин, 2 ч 15 мин, 3 д 4 ч."""
    if start is None:
        return "—"
    finish = end or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if finish.tzinfo is None:
        finish = finish.replace(tzinfo=timezone.utc)
    seconds = max(0, int((finish - start).total_seconds()))
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    days, hours = divmod(hours, 24)
    return f"{days} д {hours} ч" if hours else f"{days} д"


async def gps_mileage_for_period(
    session: AsyncSession, *, vehicle_id: int, start: datetime, end: datetime
) -> Decimal | None:
    """Пробег машины за период по счётчику трекера, км. None — данных нет."""
    from sqlalchemy import func, select

    from app.models import VehicleTelemetryPoint

    row = (
        await session.execute(
            select(
                func.min(VehicleTelemetryPoint.mileage_km),
                func.max(VehicleTelemetryPoint.mileage_km),
                func.count(VehicleTelemetryPoint.id),
            ).where(
                VehicleTelemetryPoint.vehicle_id == vehicle_id,
                VehicleTelemetryPoint.observed_at >= start,
                VehicleTelemetryPoint.observed_at <= end,
                VehicleTelemetryPoint.mileage_km.is_not(None),
                VehicleTelemetryPoint.mileage_km > 0,
            )
        )
    ).one()
    mn, mx, cnt = row
    if mn is None or mx is None or cnt < 2:
        return None
    distance = Decimal(mx) - Decimal(mn)
    return distance if distance >= 0 else None


def format_mileage_comparison(odometer_km: int, gps_km: Decimal) -> str:
    """Строка для бота: одометр против GPS + пометка при большом расхождении."""
    diff = Decimal(odometer_km) - gps_km
    base = f"📡 По GPS (Stavtrack): {gps_km:.0f} км. Расхождение: {diff:+.0f} км."
    reference = max(gps_km, Decimal(1))
    if abs(diff) / reference > MILEAGE_MISMATCH_ALERT_RATIO:
        base += " ⚠️ Больше 10% — стоит проверить."
    return base
