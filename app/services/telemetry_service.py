"""
Выжимки из GPS-телеметрии для бота и кабинета.

Пробег за период считаем по mileage_km (одометр самого трекера Stavtrack:
max − min за период), а НЕ суммой расстояний между координатами — счётчик
прибора не «прыгает», когда GPS лагает в городе, поэтому сравнение с
одометром машины честное.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VehicleTelemetryPoint

# Больше этой доли расхождение одометра и GPS считаем подозрительным.
MILEAGE_MISMATCH_ALERT_RATIO = Decimal("0.10")


async def gps_mileage_for_period(
    session: AsyncSession, *, vehicle_id: int, start: datetime, end: datetime
) -> Decimal | None:
    """Пробег машины за период по счётчику трекера, км. None — данных нет."""
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
