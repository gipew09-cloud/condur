"""
Выжимки из GPS-телеметрии для бота и кабинета.

Пробег за период считаем по mileage_km (одометр самого трекера Stavtrack:
max − min за период), а НЕ суммой расстояний между координатами — счётчик
прибора не «прыгает», когда GPS лагает в городе, поэтому сравнение с
одометром машины честное.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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


# Двигатель заведён определяем по напряжению бортсети (генератор заряжает
# АКБ), а НЕ по сырому биту ign. Инцидент 18.07.2026: у трекера Т557ОС178 в
# ретрансляции ignition=1 «залип», хотя двигатель заглушен — Stavtrack честно
# показывал «Зажигание Off» при 25.3 В. Напряжение — тот же признак, по
# которому судит Stavtrack: работает генератор → ~28 В (борт 24 В) / ~14 В
# (борт 12 В); заглушен → питание от АКБ ~25 В / ~12.6 В.
ENGINE_BOARD_POWER_MIN_V = Decimal("5")     # ниже — питание борта потеряно/нет данных
ENGINE_SYSTEM_24V_MIN_V = Decimal("18")     # выше этого — бортсеть 24 В, иначе 12 В
ENGINE_ON_THRESHOLD_24V = Decimal("27.0")   # генератор 24-В сети
ENGINE_ON_THRESHOLD_12V = Decimal("13.2")   # генератор 12-В сети


def engine_running_from_voltage(power_v) -> bool | None:
    """Двигатель заведён по напряжению бортсети. None — судить нельзя (нет
    достоверного напряжения): пусть решает сырой бит зажигания как раньше."""
    if power_v is None:
        return None
    try:
        volts = Decimal(str(power_v))
    except (TypeError, ValueError, InvalidOperation):
        return None
    if volts < ENGINE_BOARD_POWER_MIN_V:
        return None
    threshold = ENGINE_ON_THRESHOLD_24V if volts >= ENGINE_SYSTEM_24V_MIN_V else ENGINE_ON_THRESHOLD_12V
    return volts >= threshold


# Пробег трекера в Wialon приходит в параметре totalDistance в МЕТРАХ
# (проверено 18.07.2026: totalDistance=1691610321 м → 1691610.32 км, ровно
# как одометр в самом Stavtrack). В EGTS пробег уже в км (odometer_km).
# Наш столбец mileage_km — в километрах, поэтому метры делим на 1000.
def wialon_odometer_km(total_distance_m) -> Decimal | None:
    """totalDistance (метры) → пробег прибора в км. None — нет/мусор/ноль."""
    if total_distance_m is None:
        return None
    try:
        km = Decimal(str(total_distance_m)) / Decimal(1000)
    except (TypeError, ValueError, InvalidOperation):
        return None
    return km if km > 0 else None


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


# =========================================================================
# Аналитика приездов на РЦ: типичное время приезда и «быстрый час».
# =========================================================================
def typical_time_of_day_label(minutes_of_day: list[int]) -> str | None:
    """Медианное время суток «08:30» из списка минут от полуночи.

    Медиана, а не среднее: один ночной приезд не сдвигает типичное время.
    (Если приезды размазаны вокруг полуночи, медиана условна — для складов
    с дневной работой это не мешает.)
    """
    if not minutes_of_day:
        return None
    vals = sorted(minutes_of_day)
    mid = vals[len(vals) // 2]
    return f"{mid // 60:02d}:{mid % 60:02d}"


def best_arrival_hour(
    hour_waits: list[tuple[int, int]], *, min_visits: int = 2
) -> tuple[int, int] | None:
    """Час приезда, в который выгрузка в среднем самая быстрая.

    hour_waits: пары (час приезда 0..23, минут под выгрузкой).
    Часы с меньше чем min_visits приездами не участвуют (одна удачная
    выгрузка — не статистика). Возвращает (час, средние минуты) или None.
    """
    by_hour: dict[int, list[int]] = {}
    for hour, waited in hour_waits:
        by_hour.setdefault(hour, []).append(waited)
    candidates = [
        (sum(waits) // len(waits), hour)
        for hour, waits in by_hour.items()
        if len(waits) >= min_visits
    ]
    if not candidates:
        return None
    avg, hour = min(candidates)
    return hour, avg


# =========================================================================
# Хронология смены: «ехал / стоял / нет сигнала» по точкам трекера.
# Для карточки смены в кабинете — владелец видит всю историю дня.
# =========================================================================
SEGMENT_MOVE_KMH = Decimal("3")   # порог «едет» — как в vehicle_motion_status
SEGMENT_MIN_SECONDS = 180         # короче — светофор/дрожание GPS, склеиваем
SEGMENT_GAP_SECONDS = 15 * 60     # дыра между точками дольше — «нет сигнала»


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def segment_movements(
    points: list[tuple[datetime, Decimal | float | int | None]],
    *,
    window_end: datetime,
    tail_open: bool = False,
) -> list[dict]:
    """Разбить точки (observed_at, speed_kmh) на отрезки истории смены.

    Возвращает [{"kind": "move"|"stop"|"nosignal", "start", "end", "ongoing"}].
    Правила:
      - скорость > SEGMENT_MOVE_KMH → «ехал», иначе «стоял»;
      - разрыв между точками дольше SEGMENT_GAP_SECONDS → «нет сигнала»;
      - отрезки короче SEGMENT_MIN_SECONDS приклеиваются к соседям
        (остановка на светофоре не рвёт поездку, дрожание GPS не «едет»);
      - последний отрезок тянется до window_end; tail_open=True помечает его
        ongoing (смена ещё активна — «стоит/едет прямо сейчас»).
    """
    pts = sorted(
        (( _utc(t), Decimal(str(s if s is not None else 0)) ) for t, s in points if t is not None),
        key=lambda p: p[0],
    )
    if not pts:
        return []
    window_end = max(_utc(window_end), pts[-1][0])
    gap = timedelta(seconds=SEGMENT_GAP_SECONDS)

    raw: list[dict] = []

    def push(kind: str, start: datetime, end: datetime) -> None:
        if end <= start:
            return
        if raw and raw[-1]["kind"] == kind:
            raw[-1]["end"] = end
        else:
            raw.append({"kind": kind, "start": start, "end": end})

    for i, (t, speed) in enumerate(pts):
        kind = "move" if speed > SEGMENT_MOVE_KMH else "stop"
        next_t = pts[i + 1][0] if i + 1 < len(pts) else window_end
        # состояние точки «живёт» максимум gap; дальше — честное «нет сигнала»
        push(kind, t, min(next_t, t + gap))
        if next_t > t + gap:
            push("nosignal", t + gap, next_t)

    # Склейка коротких всплесков с предыдущим отрезком.
    smoothed: list[dict] = []
    for seg in raw:
        dur = (seg["end"] - seg["start"]).total_seconds()
        if smoothed and seg["kind"] != "nosignal" and dur < SEGMENT_MIN_SECONDS:
            smoothed[-1]["end"] = seg["end"]
            continue
        if smoothed and smoothed[-1]["kind"] == seg["kind"]:
            smoothed[-1]["end"] = seg["end"]
        else:
            smoothed.append(dict(seg))
    # Короткий первый отрезок вливаем во второй (иначе минутный «выезд»
    # от дрожания GPS выглядел бы как настоящий).
    if len(smoothed) >= 2:
        first = smoothed[0]
        if (
            first["kind"] != "nosignal"
            and (first["end"] - first["start"]).total_seconds() < SEGMENT_MIN_SECONDS
        ):
            smoothed[1]["start"] = first["start"]
            smoothed.pop(0)

    for seg in smoothed:
        seg["ongoing"] = False
    if tail_open and smoothed:
        smoothed[-1]["ongoing"] = True
    return smoothed


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


# =========================================================================
# Длительность простоя на РЦ = НЕПРЕРЫВНОЕ пребывание в геозоне по GPS.
# Считаем по факту (был ли трекер внутри радиуса), а НЕ по времени начала
# текущей стоянки: в момент отъезда машина уже трогается, и «время текущей
# стоянки» = пара минут — отсюда был баг «стоял 3 мин», хотя приехал час
# назад. И не по старому событию «приезд»: если прошлый «отъезд» потерялся,
# был фантомный «12 ч». Пребывание по точкам чинит оба случая сразу.
# =========================================================================
def rc_presence_start_from_points(
    points: list[tuple[datetime, float | None]],
    exit_radius_m: float,
    tolerate_outside: int = 1,
) -> datetime | None:
    """Начало текущего непрерывного пребывания в геозоне РЦ.

    points — [(observed_at, расстояние_до_центра_РЦ_м)] по возрастанию времени.
    Идём от свежих точек к старым: сначала пропускаем «хвост снаружи» (машина
    как раз выезжает или уже выехала), затем берём непрерывный отрезок «внутри»
    и возвращаем его начало. Одиночные выбросы GPS наружу (не больше
    tolerate_outside подряд) считаем шумом. Устойчивый выход наружу ДО этого
    отрезка обрывает счёт — стоянка не склеивается с прошлым визитом.
    None — точек внутри геозоны нет (тогда вызывающий берёт запасное время).
    """
    start: datetime | None = None
    in_run = False
    outside_streak = 0
    for observed_at, dist in reversed(points):
        inside = dist is not None and dist <= exit_radius_m
        if not in_run:
            if inside:
                in_run = True
                start = observed_at
            continue  # ещё «хвост снаружи» — пропускаем
        if inside:
            start = observed_at
            outside_streak = 0
        else:
            outside_streak += 1
            if outside_streak > tolerate_outside:
                break
    return start


async def rc_presence_started_at(
    session: AsyncSession, *, vehicle_id: int, rc_lat, rc_lon,
    exit_radius_m: float, now: datetime, fallback: datetime,
    lookback_hours: int = 48,
) -> datetime:
    """Когда машина начала текущую непрерывную стоянку в геозоне РЦ (по GPS).
    fallback — запасное время, если достоверных точек нет."""
    from sqlalchemy import select

    from app.models import VehicleTelemetryPoint
    from app.services import rc_service

    rows = (
        await session.execute(
            select(
                VehicleTelemetryPoint.observed_at,
                VehicleTelemetryPoint.latitude,
                VehicleTelemetryPoint.longitude,
            )
            .where(
                VehicleTelemetryPoint.vehicle_id == vehicle_id,
                VehicleTelemetryPoint.is_valid.is_(True),
                VehicleTelemetryPoint.observed_at.is_not(None),
                VehicleTelemetryPoint.observed_at >= now - timedelta(hours=lookback_hours),
                VehicleTelemetryPoint.observed_at <= now,
                VehicleTelemetryPoint.latitude.is_not(None),
                VehicleTelemetryPoint.longitude.is_not(None),
            )
            .order_by(VehicleTelemetryPoint.observed_at)
            .limit(20000)
        )
    ).all()
    points = [
        (t, rc_service.haversine_m(lat, lon, rc_lat, rc_lon))
        for t, lat, lon in rows
    ]
    return rc_presence_start_from_points(points, exit_radius_m) or fallback


def format_mileage_comparison(odometer_km: int, gps_km: Decimal) -> str:
    """Строка для бота: одометр против GPS + пометка при большом расхождении."""
    diff = Decimal(odometer_km) - gps_km
    base = f"📡 По GPS (Stavtrack): {gps_km:.0f} км. Расхождение: {diff:+.0f} км."
    reference = max(gps_km, Decimal(1))
    if abs(diff) / reference > MILEAGE_MISMATCH_ALERT_RATIO:
        base += " ⚠️ Больше 10% — стоит проверить."
    return base


# =========================================================================
# Зажигание: «завёл/заглушил двигатель» — переходы и состояние на момент.
# Для уведомлений о начале/конце смены и хронологии в кабинете.
# =========================================================================
IGNITION_FLICKER_SECONDS = 60      # состояние короче — дребезг/кривой пакет
IGNITION_FRESH_MINUTES = 15        # последняя точка старее — состояние не знаем
IGNITION_LOOKBACK_HOURS = 12       # сколько истории смотрим назад


def _ignition_runs(
    points: list[tuple[datetime, bool | None]],
    flicker_seconds: int = IGNITION_FLICKER_SECONDS,
) -> list[dict]:
    """Непрерывные отрезки одного состояния зажигания по точкам
    (observed_at, ignition). Точки с ignition=None пропускаем — датчик не
    пришёл, не выдумываем. Отрезок короче flicker_seconds, зажатый между
    двумя одинаковыми соседями, вливаем в них: одиночный кривой пакет не
    должен рождать «завёл/заглушил». Последний отрезок не трогаем — текущее
    состояние честное, даже если ему пара секунд.
    Возвращает [{"on": bool, "first": dt, "last": dt}] по времени.
    """
    known = sorted(
        ((_utc(t), bool(ign)) for t, ign in points if t is not None and ign is not None),
        key=lambda p: p[0],
    )
    runs: list[dict] = []
    for t, on in known:
        if runs and runs[-1]["on"] == on:
            runs[-1]["last"] = t
        else:
            runs.append({"on": on, "first": t, "last": t})

    # Склейка дребезга — итеративно, БЕЗ рекурсии: болтающийся контакт датчика
    # может дать тысячи коротких отрезков подряд, рекурсия бы упала по глубине
    # (а это уронило бы открытие смены в боте).
    changed = True
    while changed:
        changed = False
        i = 1
        while i < len(runs) - 1:
            mid = runs[i]
            if (
                (mid["last"] - mid["first"]).total_seconds() < flicker_seconds
                and runs[i - 1]["on"] == runs[i + 1]["on"]
            ):
                runs[i - 1]["last"] = runs[i + 1]["last"]
                del runs[i : i + 2]
                changed = True
            else:
                i += 1
    return runs


def ignition_transitions(
    points: list[tuple[datetime, bool | None]],
    flicker_seconds: int = IGNITION_FLICKER_SECONDS,
) -> list[dict]:
    """Моменты «завёл двигатель» / «заглушил двигатель».

    Возвращает [{"at": dt, "on": bool}]: on=True — завёл, False — заглушил.
    Момент перехода — первая точка нового состояния (точнее по данным не
    узнать: между точками трекер молчал).
    """
    runs = _ignition_runs(points, flicker_seconds)
    return [{"at": run["first"], "on": run["on"]} for run in runs[1:]]


def ignition_state_at(
    points: list[tuple[datetime, bool | None]],
    moment: datetime,
    fresh_minutes: int = IGNITION_FRESH_MINUTES,
) -> dict | None:
    """Состояние зажигания на момент moment по точкам (observed_at, ignition).

    None — данных нет или последняя точка старее fresh_minutes (трекер молчит —
    не выдумываем). Иначе {"on": bool, "since": dt, "since_exact": bool}:
    since — с какого времени это состояние; since_exact=False — состояние
    длилось уже на первой точке окна, реальное начало раньше («не меньше …»).
    """
    moment = _utc(moment)
    runs = _ignition_runs([(t, ign) for t, ign in points if t is not None and _utc(t) <= moment])
    if not runs:
        return None
    last = runs[-1]
    if (moment - last["last"]).total_seconds() > fresh_minutes * 60:
        return None
    return {"on": last["on"], "since": last["first"], "since_exact": len(runs) > 1}


async def shift_ignition_snapshot(
    session: AsyncSession, *, vehicle_id: int, moment: datetime
) -> dict | None:
    """Состояние зажигания машины на момент открытия/закрытия смены.

    Одна выборка двух колонок за IGNITION_LOOKBACK_HOURS, вызывается дважды
    за смену — бота не нагружает. None — датчик зажигания не приходит.
    """
    from sqlalchemy import select

    from app.models import VehicleTelemetryPoint

    rows = (
        await session.execute(
            select(VehicleTelemetryPoint.observed_at, VehicleTelemetryPoint.ignition)
            .where(
                VehicleTelemetryPoint.vehicle_id == vehicle_id,
                VehicleTelemetryPoint.ignition.is_not(None),
                VehicleTelemetryPoint.observed_at.is_not(None),
                VehicleTelemetryPoint.observed_at >= moment - timedelta(hours=IGNITION_LOOKBACK_HOURS),
                VehicleTelemetryPoint.observed_at <= moment,
            )
            .order_by(VehicleTelemetryPoint.observed_at)
            .limit(20000)
        )
    ).all()
    return ignition_state_at([(t, ign) for t, ign in rows], moment)


def ignition_shift_line(
    snapshot: dict | None, *, moment: datetime, tz_name: str | None, closing: bool
) -> str | None:
    """Строка о двигателе для уведомления владельцу о смене.

    None — датчик зажигания не приходит: строку не пишем вовсе, чтобы у машин
    без wialon-ретрансляции уведомления не обрастали «нет данных».
    """
    from app.services.timeutil import smart_since_label

    if snapshot is None:
        return None
    moment = _utc(moment)
    on, since, exact = snapshot["on"], snapshot["since"], snapshot["since_exact"]
    ago = duration_label(since, moment)
    when = smart_since_label(since, tz_name)  # «с 07:58» / «со вчера, 21:52»
    just_now = (moment - since).total_seconds() < 60

    if not closing:  # уведомление о НАЧАЛЕ смены
        if on:
            if not exact:
                return f"🔑 Двигатель работает — уже не меньше {ago}."
            if just_now:
                return "🔑 Двигатель завели прямо перед началом смены."
            return f"🔑 Двигатель работает {when} — завели за {ago} до начала смены."
        if not exact:
            return f"🔑 Двигатель не заведён (заглушен уже не меньше {ago})."
        return f"🔑 Двигатель пока не заведён (заглушен {when})."

    # уведомление о ЗАВЕРШЕНИИ смены
    if on:
        return "🔑 Двигатель ещё работает" + (f" ({when})." if exact else ".")
    if not exact:
        return f"🔑 Двигатель заглушен — уже не меньше {ago}."
    if just_now:
        return "🔑 Двигатель заглушен прямо перед завершением смены."
    return f"🔑 Двигатель заглушен {when} — за {ago} до завершения смены."
