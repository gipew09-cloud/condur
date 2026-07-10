"""
Выжимки из GPS-телеметрии для бота и кабинета.

Пробег за период считаем по mileage_km (одометр самого трекера Stavtrack:
max − min за период), а НЕ суммой расстояний между координатами — счётчик
прибора не «прыгает», когда GPS лагает в городе, поэтому сравнение с
одометром машины честное.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Больше этой доли расхождение одометра и GPS считаем подозрительным.
MILEAGE_MISMATCH_ALERT_RATIO = Decimal("0.10")

# После 12 часов в геозоне РЦ считаем простой потенциально платным.
# Пока это только сигнал владельцу и статистика, без автозаписи в финансы:
# GPS/геозона могут ошибиться, поэтому деньги должен подтвердить человек.
RC_BILLABLE_WAIT_MINUTES = 12 * 60
RC_BILLABLE_DOWNTIME_RUB = 8000

MOTION_MOVING = "moving"
MOTION_IDLE_ENGINE = "idle_engine"
MOTION_STOPPED = "stopped"
MOTION_UNKNOWN = "unknown"

SIGNAL_OK = "ok"
SIGNAL_GPS_STALE = "gps_stale"
SIGNAL_GPS_INVALID = "gps_invalid"
SIGNAL_MOVING_WITHOUT_SHIFT = "moving_without_shift"
SIGNAL_MOVING_WITHOUT_TRIP = "moving_without_trip"
SIGNAL_IDLE_ENGINE = "idle_engine"


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


def vehicle_control_signal(
    *,
    motion_status: str | None,
    has_active_shift: bool,
    has_active_trip: bool,
    gps_stale: bool = False,
    gps_invalid: bool = False,
) -> str:
    """Главный GPS-сигнал для владельца: что требует внимания прямо сейчас."""
    if gps_stale:
        return SIGNAL_GPS_STALE
    if gps_invalid:
        return SIGNAL_GPS_INVALID
    if motion_status == MOTION_MOVING and not has_active_shift:
        return SIGNAL_MOVING_WITHOUT_SHIFT
    if motion_status == MOTION_MOVING and not has_active_trip:
        return SIGNAL_MOVING_WITHOUT_TRIP
    if motion_status == MOTION_IDLE_ENGINE:
        return SIGNAL_IDLE_ENGINE
    return SIGNAL_OK


def parked_long_enough(
    motion_status: str | None,
    motion_since_at: datetime | None,
    now: datetime,
    min_minutes: int,
) -> bool:
    """Машина реально СТОИТ (не едет) уже минимум min_minutes.

    Ключ к геозонам без ложных срабатываний: грузовик, проезжающий мимо РЦ
    по соседней дороге (или вставший на светофоре на пару минут), не должен
    считаться «приехавшим». Стоянка = stopped или idle_engine; отсчёт — от
    motion_since_at (когда текущее состояние началось).
    """
    if motion_status not in (MOTION_STOPPED, MOTION_IDLE_ENGINE):
        return False
    if motion_since_at is None:
        return False
    if motion_since_at.tzinfo is None:
        motion_since_at = motion_since_at.replace(tzinfo=timezone.utc)
    return (now - motion_since_at) >= timedelta(minutes=min_minutes)


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


def int_or_none(value) -> int | None:
    """Безопасно привести значение из JSON/env/form к int.

    В events.payload значения обычно числа, но после ручных правок/старых версий
    там могут оказаться строки или мусор. Для статистики лучше показать прочерк,
    чем уронить страницу владельца.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def minutes_label(minutes) -> str:
    value = int_or_none(minutes)
    if value is None:
        return "—"
    value = max(0, value)
    if value < 60:
        return f"{value} мин"
    hours, mins = divmod(value, 60)
    return f"{hours} ч {mins} мин" if mins else f"{hours} ч"


def rub_label(amount) -> str:
    value = int_or_none(amount) or 0
    if value <= 0:
        return "—"
    return f"{value:,}".replace(",", " ") + " ₽"


def rc_billable_downtime_rub(waited_minutes) -> int:
    value = int_or_none(waited_minutes)
    if value is None or value < RC_BILLABLE_WAIT_MINUTES:
        return 0
    blocks = value // RC_BILLABLE_WAIT_MINUTES
    return blocks * RC_BILLABLE_DOWNTIME_RUB


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


def sum_engine_off_seconds(
    points: list[tuple[datetime, bool | None]],
    gap_cap_seconds: int = 600,
) -> int:
    """Сколько секунд двигатель был ВЫКЛЮЧЕН по последовательности точек
    (observed_at, ignition). Интервал между соседними точками приписываем
    состоянию первой; дыры длиннее gap_cap_seconds не приписываем никому
    (трекер молчал — не знаем, что было)."""
    total = 0
    for (t1, ign1), (t2, _ign2) in zip(points, points[1:]):
        if t1 is None or t2 is None:
            continue
        delta = (t2 - t1).total_seconds()
        if delta <= 0 or delta > gap_cap_seconds:
            continue
        if ign1 is False:
            total += int(delta)
    return total


def steady_moving_vehicle_ids(
    moving_points: list[tuple[int, datetime | None]],
    now: datetime,
    min_minutes: int,
) -> set[int]:
    """ID машин, которые едут ДОЛЬШЕ min_minutes — фильтр от кратких скачков GPS
    (одиночный «прыжок» скорости не должен слать напоминание «начни смену»).
    moving_points: список (vehicle_id, motion_since_at)."""
    cutoff = now - timedelta(minutes=min_minutes)
    result: set[int] = set()
    for vid, since in moving_points:
        if since is None:
            continue
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if since <= cutoff:
            result.add(vid)
    return result


def engine_off_minutes_from_points(
    points: list[tuple[datetime, bool | None]],
) -> int | None:
    """Минуты с заглушенным двигателем по точкам (observed_at, ignition).

    ВАЖНО (см. NEXT_SESSION_PROMPT.md, разбор EGTS): датчик зажигания в
    ретрансляции Stavtrack пока НЕ приходит — парсер даёт ignition только
    True или None, но никогда False. Пока в данных нет НИ ОДНОЙ точки с
    ignition=False, честно возвращаем None («нет данных»), а НЕ 0 — иначе
    в статистике простоя и в счетах за простой будет ложь. Когда датчик
    включат в Stavtrack и пойдут реальные False — функция сама начнёт
    считать настоящие минуты.
    """
    known = [(t, ign) for t, ign in points if ign is not None]
    if len(known) < 2:
        return None
    if not any(ign is False for _, ign in known):
        return None
    return sum_engine_off_seconds(known) // 60


async def engine_off_minutes(
    session: AsyncSession, *, vehicle_id: int, start: datetime, end: datetime
) -> int | None:
    """Минуты с заглушенным двигателем в интервале, по точкам телеметрии.
    None — датчик зажигания «выкл» не приходит (см. engine_off_minutes_from_points)."""
    from sqlalchemy import select

    from app.models import VehicleTelemetryPoint

    rows = (
        await session.execute(
            select(VehicleTelemetryPoint.observed_at, VehicleTelemetryPoint.ignition)
            .where(
                VehicleTelemetryPoint.vehicle_id == vehicle_id,
                VehicleTelemetryPoint.observed_at >= start,
                VehicleTelemetryPoint.observed_at <= end,
                VehicleTelemetryPoint.ignition.is_not(None),
            )
            .order_by(VehicleTelemetryPoint.observed_at)
        )
    ).all()
    return engine_off_minutes_from_points([(t, ign) for t, ign in rows])


def format_mileage_comparison(odometer_km: int, gps_km: Decimal) -> str:
    """Строка для бота: одометр против GPS + пометка при большом расхождении."""
    diff = Decimal(odometer_km) - gps_km
    base = f"📡 По GPS (Stavtrack): {gps_km:.0f} км. Расхождение: {diff:+.0f} км."
    reference = max(gps_km, Decimal(1))
    if abs(diff) / reference > MILEAGE_MISMATCH_ALERT_RATIO:
        base += " ⚠️ Больше 10% — стоит проверить."
    return base
