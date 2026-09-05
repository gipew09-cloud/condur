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


def engine_running_from_params(params: dict | None) -> bool | None:
    """Работает ли двигатель, по любым признакам, что прислал терминал.

    Порядок: сначала напряжение бортсети (генератор заряжает АКБ), потом
    прямой флаг `generator` навтелекома. У нового терминала «СМАРТ» поля
    `ignition` нет вообще, а `power` приходит нулём, когда выключена масса, —
    без этого разбора машина навсегда застревала в «зажигание не передано».
    """
    if not params:
        return None
    by_voltage = engine_running_from_voltage(params.get("power"))
    if by_voltage is not None:
        return by_voltage
    generator = params.get("generator")
    if generator in (0, 1, True, False):
        return bool(generator)
    return None


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


# Значения-заглушки в потоке Ставтрэка: незаполненные каналы приходят как
# 65535 / -128 / -327.68. Это «нет датчика», а не показание — пускать такое
# в интерфейс нельзя (владелец увидит «65535 В» и перестанет верить экрану).
SENSOR_SENTINELS = (65535, 65534, -128, -327.68, -3276.8)
SENSOR_VOLTAGE_MAX_V = Decimal("100")


def sensor_voltage(value) -> Decimal | None:
    """Напряжение из параметров трекера. None — нет датчика или мусор.

    ⚠️ Ноль здесь — тоже None: для ДАТЧИКА (своя батарея трекера) ноль вольт
    означает «канал пустой». Для БОРТСЕТИ это не так — см. `bus_voltage`.
    """
    if value is None:
        return None
    try:
        volts = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None
    if any(volts == Decimal(str(sentinel)) for sentinel in SENSOR_SENTINELS):
        return None
    if volts <= 0 or volts > SENSOR_VOLTAGE_MAX_V:
        return None
    return volts


# Ниже этого бортсеть считаем отсутствующей, а не «маленькой»: трекер шлёт
# ровно 0, когда выключена масса.
BUS_VOLTAGE_ABSENT_V = Decimal("0.5")


def bus_voltage(value) -> Decimal | None:
    """Напряжение БОРТСЕТИ. Ноль — это показание «сети нет», а не «нет данных».

    ⚠️ Владелец 04.09.2026: «в Ставтрэке на 557 напряжения нет, а у нас есть».
    Так и было: терминал шлёт `power = 0`, когда выключена масса, старый разбор
    превращал ноль в «нет данных», в быстрый слой не попадало ничего, и кабинет
    подставлял на карточку ПРОШЛОЕ напряжение из истории. Машина стояла
    обесточенная, а экран показывал 27 В.

    Ноль вольт — это факт, который и нужен владельцу: масса выключена, трекер
    доживает на своей батарее. Возвращаем его как показание.
    """
    if value is None:
        return None
    try:
        volts = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None
    if any(volts == Decimal(str(sentinel)) for sentinel in SENSOR_SENTINELS):
        return None
    if volts < 0 or volts > SENSOR_VOLTAGE_MAX_V:
        return None
    return Decimal("0") if volts < BUS_VOLTAGE_ABSENT_V else volts


# Уровень топлива с ДУТ приходит в параметрах fuel1…fuel15, температура — в
# fuelTemp1…fuelTemp15. По документации УМКа302: 1…7 — проводные каналы,
# 8…15 — беспроводные BLE-датчики. Незанятый канал шлёт заглушку 65535
# (температура — -128). У Т557ОС178 датчик сидит на канале 2 → fuel2/fuelTemp2,
# но канал жёстко не зашиваем: на другой машине он может оказаться другим.
FUEL_CHANNELS = tuple(range(1, 16))
FUEL_RAW_MAX = Decimal("60000")     # выше — заведомо мусор, а не показание
FUEL_TEMP_MIN = Decimal("-60")
FUEL_TEMP_MAX = Decimal("120")


def _clean_number(value):
    """Число из параметров трекера без заглушек. None — датчика нет или мусор."""
    if value is None:
        return None
    try:
        num = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None
    if any(num == Decimal(str(sentinel)) for sentinel in SENSOR_SENTINELS):
        return None
    return num


def fuel_level_raw(params: dict | None) -> Decimal | None:
    """Сырой уровень топлива с первого живого канала ДУТ.

    Возвращает единицы датчика, НЕ литры: перевод требует тарировочной таблицы
    бака. Показывать это число владельцу нельзя — только хранить и считать по
    нему разницу, когда таблица появится.
    """
    if not params:
        return None
    for i in FUEL_CHANNELS:
        num = _clean_number(params.get(f"fuel{i}"))
        if num is None:
            continue
        if num < 0 or num > FUEL_RAW_MAX:
            continue
        return num
    return None


def fuel_temp_c(params: dict | None) -> Decimal | None:
    """Температура топлива с того же канала, где нашёлся уровень.

    Разные терминалы называют её по-разному: FleetGuide шлёт `fuelTemp2`,
    навтелеком «СМАРТ» — `temp_rs485_1` (датчик висит на шине RS-485).
    Поэтому пробуем оба имени, иначе на новой машине температура пропадает.
    """
    if not params:
        return None
    for i in FUEL_CHANNELS:
        if _clean_number(params.get(f"fuel{i}")) is None:
            continue
        for key in (f"fuelTemp{i}", f"temp_rs485_{i}"):
            num = _clean_number(params.get(key))
            if num is None:
                continue
            if FUEL_TEMP_MIN <= num <= FUEL_TEMP_MAX:
                return num
        return None
    return None


def parse_fuel_calibration(text: str | None) -> list[list[float]] | None:
    """Тарировка из текста, как её видно в Ставтрэке: пары «X Y» по строкам.

    Принимаем что угодно разумное: «417 50», «417,50», «417;50», с табами.
    Возвращаем пары, отсортированные по X, без дублей. None — пусто.
    Кидаем ValueError с понятным текстом, если строка не разбирается: владелец
    вводит это руками, и «просто не сохранилось» — худший из возможных ответов.
    """
    if not text or not text.strip():
        return None
    pairs: dict[float, float] = {}
    for num, raw_line in enumerate(text.strip().splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        parts = [p for p in line.replace(",", " ").replace(";", " ").replace("\t", " ").split(" ") if p]
        if len(parts) != 2:
            raise ValueError(f"Строка {num}: нужно два числа через пробел, а получилось «{line}»")
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError:
            raise ValueError(f"Строка {num}: «{line}» — это не числа")
        if x < 0 or y < 0:
            raise ValueError(f"Строка {num}: отрицательные значения не бывают")
        pairs[x] = y
    if len(pairs) < 2:
        raise ValueError("Нужно минимум две точки: пустой бак и полный")
    return [[x, pairs[x]] for x in sorted(pairs)]


def fuel_litres(raw, calibration) -> Decimal | None:
    """Сырое значение датчика → литры по тарировочной таблице.

    Между соседними точками считаем по прямой (так же, как Ставтрэк: у него
    на каждый отрезок свои коэффициенты a и b, что и есть уравнение прямой).
    За пределами таблицы НЕ экстраполируем — прижимаем к краю: датчик у дна и
    у горловины врёт, выдумывать там литры нельзя.
    """
    if raw is None or not calibration:
        return None
    try:
        points = sorted(
            (Decimal(str(x)), Decimal(str(y)))
            for x, y in calibration
        )
    except (TypeError, ValueError, InvalidOperation):
        return None
    if len(points) < 2:
        return None
    try:
        value = Decimal(str(raw))
    except (TypeError, ValueError, InvalidOperation):
        return None

    if value <= points[0][0]:
        return points[0][1]
    if value >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return y1
            share = (value - x0) / (x1 - x0)
            return (y0 + (y1 - y0) * share).quantize(Decimal("0.1"))
    return None


# Разбор кривой уровня топлива. Датчик шумит: на стоянке 25.08 показания
# гуляли в пределах ~4 литров без всякого движения. Поэтому мелкие колебания
# гасим, иначе «расход» набежит на пустом месте.
FUEL_NOISE_L = Decimal("1.5")        # меньше — шум датчика, не расход
FUEL_REFUEL_MIN_L = Decimal("15")    # рост больше — это заправка
FUEL_DRAIN_MIN_L = Decimal("20")     # падение больше за короткое время — похоже на слив
FUEL_DRAIN_MAX_MINUTES = 15


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def smooth_fuel(series: list[Decimal], window: int = 5) -> list[Decimal]:
    """Сгладить кривую уровня скользящей медианой.

    Медиана, а не среднее: одиночный выброс датчика (а они есть) среднее сдвинет,
    медиану — нет.
    """
    if window < 3 or len(series) < window:
        return list(series)
    half = window // 2
    out: list[Decimal] = []
    for i in range(len(series)):
        lo, hi = max(0, i - half), min(len(series), i + half + 1)
        out.append(_median(series[lo:hi]))
    return out


def fuel_runs(levels: list[Decimal], noise: Decimal) -> list[tuple[int, int, Decimal]]:
    """Свернуть кривую уровня в участки монотонного движения.

    Возвращает [(индекс начала, индекс конца, изменение)]. Разворотом считаем
    только уход от достигнутого экстремума больше чем на `noise` — иначе
    дрожание датчика нарежет кривую на сотни микроучастков.

    ⚠️ Экстремум двигаем только при СТРОГОМ улучшении. Иначе на ровном месте
    (уровень стоит) конец участка уползал вперёд, и слив «за 5 минут»
    превращался в слив «за 19 минут» — то есть переставал быть сливом.
    """
    if len(levels) < 2:
        return []
    runs: list[tuple[int, int, Decimal]] = []
    hi = lo = start = peak = 0
    direction = 0
    for i in range(1, len(levels)):
        if direction == 0:
            # Ещё не знаем, куда идём: помним самую высокую и самую низкую
            # точку — от них и начнётся участок, когда движение определится.
            if levels[i] > levels[hi]:
                hi = i
            if levels[i] < levels[lo]:
                lo = i
            if levels[i] - levels[lo] >= noise:
                start, peak, direction = lo, i, 1
            elif levels[hi] - levels[i] >= noise:
                start, peak, direction = hi, i, -1
            continue
        if direction > 0:
            if levels[i] > levels[peak]:
                peak = i
            elif levels[peak] - levels[i] >= noise:
                runs.append((start, peak, levels[peak] - levels[start]))
                start, peak, direction = peak, i, -1
        else:
            if levels[i] < levels[peak]:
                peak = i
            elif levels[i] - levels[peak] >= noise:
                runs.append((start, peak, levels[peak] - levels[start]))
                start, peak, direction = peak, i, 1
    if direction != 0:
        runs.append((start, peak, levels[peak] - levels[start]))
    return runs


def fuel_summary(points, calibration) -> dict | None:
    """Расход, заправки и подозрения на слив за период.

    points — [(observed_at, сырое значение датчика)] по возрастанию времени.

    ⚠️ Расход считаем ПО БАЛАНСУ: сколько было, минус сколько стало, плюс
    сколько залили. Раньше складывались все спуски кривой, а мелкие подъёмы
    объявлялись шумом и выбрасывались. Датчик в тряске гуляет вверх-вниз —
    и каждый ложный спуск капал в расход, а компенсирующий подъём не
    возвращался. За сутки набегали лишние литры: владелец 05.09.2026 поймал
    расхождение со Ставтрэком примерно в 5 литров и сам назвал причину —
    «программа считает, когда убавляется, а топливо иногда прибавляется».

    Заправку ищем не по одной дельте, а по НЕПРЕРЫВНОМУ подъёму (`fuel_runs`):
    налив на 300 литров растянут на несколько точек, и по одному шагу его
    можно было не узнать.

    ⚠️ Если топливо слили, слив тоже попадёт в «расход»: по датчику это
    неотличимо. Поэтому подозрительные падения отдаются рядом отдельным
    списком — решает человек.
    """
    if not calibration or not points:
        return None
    rows = []
    for observed_at, raw in points:
        litres = fuel_litres(raw, calibration)
        if litres is not None and observed_at is not None:
            rows.append((_utc(observed_at), litres))
    if len(rows) < 2:
        return None
    rows.sort(key=lambda r: r[0])
    smoothed = smooth_fuel([litres for _, litres in rows])

    refuelled = Decimal(0)
    refuels: list[dict] = []
    drains: list[dict] = []
    for start, end, delta in fuel_runs(smoothed, FUEL_NOISE_L):
        minutes = (rows[end][0] - rows[start][0]).total_seconds() / 60
        if delta > 0:
            # Мелкий подъём — плескание в баке на неровностях, не заправка.
            if delta >= FUEL_REFUEL_MIN_L:
                refuelled += delta
                refuels.append({
                    "at": rows[end][0].isoformat(),
                    "litres": float(delta.quantize(Decimal("0.1"))),
                })
            continue
        drop = -delta
        if drop >= FUEL_DRAIN_MIN_L and minutes <= FUEL_DRAIN_MAX_MINUTES:
            drains.append({
                "at": rows[end][0].isoformat(),
                "litres": float(drop.quantize(Decimal("0.1"))),
                "minutes": round(minutes),
            })

    # Баланс бака. Отрицательного расхода не бывает: если он получился, значит
    # долив не опознан как заправка — честнее показать ноль, чем минус.
    spent = smoothed[0] - smoothed[-1] + refuelled
    if spent < 0:
        spent = Decimal(0)
    return {
        "start_l": float(smoothed[0].quantize(Decimal("0.1"))),
        "end_l": float(smoothed[-1].quantize(Decimal("0.1"))),
        "spent_l": float(spent.quantize(Decimal("0.1"))),
        "refuelled_l": float(refuelled.quantize(Decimal("0.1"))),
        "refuels": refuels,
        "drains": drains,
        "points": len(rows),
    }


# Массу выключили: бортсеть просела почти до нуля, а трекер ещё живёт на своей
# батарее и продолжает слать точки. Снаружи это выглядит как обычная стоянка —
# именно так теряются машины (PROBLEMS №21). Порог 5 В: ниже него бортсети
# фактически нет ни у 12-, ни у 24-вольтовой машины.
POWER_CUT_MAX_V = Decimal("5")


def power_cut(voltage, battery_voltage=None) -> bool:
    """True — машина обесточена (масса выключена), трекер на своей батарее.

    ⚠️ Напряжение читаем через `bus_voltage`: раньше здесь стоял
    `sensor_voltage`, который ноль превращал в None, — и признак «обесточена»
    не срабатывал именно в том случае, ради которого писался (терминал шлёт
    ровно 0, когда сняли массу).
    """
    volts = bus_voltage(voltage)
    if volts is None:
        return False
    if volts >= POWER_CUT_MAX_V:
        return False
    # Точки продолжают идти — значит трекер жив, питается от своей батареи.
    return True


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

# Проверка ЗДРАВОГО СМЫСЛА для соседних точек трека.
# Ставтрэк шлёт точки примерно раз в 30–40 секунд. Если между двумя точками
# прошло сильно больше и машина при этом заметно сместилась — мы НЕ ЗНАЕМ,
# каким путём она ехала: прямая линия между ними прошла бы «сквозь дома».
# Такой участок рисуем пунктиром и в пробег не считаем.
TRACK_LEG_GAP_SECONDS = 120       # больше двух минут между точками — пропуск
TRACK_LEG_MAX_M = 400             # ближе 400 м прямая линия — безобидное упрощение
TRACK_LEG_MAX_KMH = 150           # быстрее — физически невозможно, это скачок


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


def gps_jump_reason(prev_at, prev_lat, prev_lon, at, lat, lon) -> str | None:
    """«Скачок GPS»: метка улетела и вернулась. Возвращает причину или None.

    Владелец 04.09.2026: «точка телепортирует на секунду в какое-то другое
    место и обратно на машину». Это не ошибка отрисовки — такие точки реально
    приходят: у Пулково и рядом с военными объектами сигнал глушат и
    подменяют, и трекер честно рапортует чужие координаты с признаком
    «достоверно». Раньше такая точка ложилась в «быстрый слой», и метка на
    карте прыгала; хуже того — на ней срабатывали геозоны РЦ.

    Судим ТОЛЬКО по соседним во времени точкам (не дальше
    TRACK_LEG_GAP_SECONDS). Если между точками прошёл час, машина могла честно
    уехать далеко — и объявлять это скачком нельзя.

    ⚠️ Это не сглаживание. Точку мы не двигаем и не выдумываем: помечаем
    недостоверной и не пускаем в геометрию, как уже делаем с «нулевым
    островом» (0, 0).
    """
    from app.services import rc_service

    if None in (prev_at, prev_lat, prev_lon, at, lat, lon):
        return None
    seconds = (at - prev_at).total_seconds()
    if seconds <= 0 or seconds > TRACK_LEG_GAP_SECONDS:
        return None
    metres = rc_service.haversine_m(
        float(prev_lat), float(prev_lon), float(lat), float(lon)
    )
    if metres <= TRACK_LEG_MAX_M:
        return None
    kmh = (metres / seconds) * 3.6
    if kmh <= TRACK_LEG_MAX_KMH:
        return None
    return (
        f"скачок GPS: {metres / 1000:.1f} км за {int(seconds)} с "
        f"({int(kmh)} км/ч)"
    )


def _implausible_leg_kmh(a, b) -> float | None:
    """Скорость между двумя точками, если её вообще можно осудить.

    None — судить нельзя: точки далеко по времени (машина могла честно уехать)
    или сместилась в пределах погрешности.
    """
    from app.services import rc_service

    seconds = (b[0] - a[0]).total_seconds()
    if seconds <= 0 or seconds > TRACK_LEG_GAP_SECONDS:
        return None
    metres = rc_service.haversine_m(a[1], a[2], b[1], b[2])
    if metres <= TRACK_LEG_MAX_M:
        return None
    return (metres / seconds) * 3.6


# Насколько далеко участок должен «уехать» от соседей, чтобы считать его
# подменой, а не погрешностью: 20 км — это уже другой город.
UNREACHABLE_MIN_M = 20_000
# Дальше этого по времени судить нельзя: за 15 минут молчания машина могла
# честно уехать куда угодно.
UNREACHABLE_JUDGE_SECONDS = 15 * 60


def _leg_impossible(a, b) -> bool:
    """Между двумя точками машина оказаться не могла — при любой скорости."""
    from app.services import rc_service

    seconds = (b[0] - a[0]).total_seconds()
    if seconds <= 0 or seconds > UNREACHABLE_JUDGE_SECONDS:
        return False                      # долго молчал — судить не о чем
    metres = rc_service.haversine_m(a[1], a[2], b[1], b[2])
    if metres <= TRACK_LEG_MAX_M:
        return False
    return (metres / seconds) * 3.6 > TRACK_LEG_MAX_KMH


def drop_unreachable_runs(points: list[tuple]) -> tuple[list[tuple], list[dict]]:
    """Выбросить УЧАСТКИ, куда машина попасть не могла, и вернуться обратно.

    Владелец 04.09.2026: «у нас обычно геолокацию глушат на вот этот остров, и
    машина действительно в какой-то момент попала на этот остров и покаталась
    по кругу». Это не одна битая точка (её ловил прошлый фильтр), а целая
    пачка правдоподобных точек в чужом месте: подменённый сигнал ведёт себя
    как настоящий — едет, поворачивает, стоит.

    Опознаём по трём признакам сразу:
      1. хотя бы одна граница участка — физически невозможный переход
         (`_leg_impossible`): 100 км за минуту не бывает;
      2. участок дальше UNREACHABLE_MIN_M от того, что было ДО и ПОСЛЕ него;
      3. соседи при этом рядом друг с другом — машина «вернулась» туда, откуда
         «улетела».

    ⚠️ Одного «далеко» мало: машина может честно уехать за 100 км, пока
    трекер молчит. Поэтому пункт 1 обязателен — без доказанной невозможности
    точки остаются на месте, а разрыв рисуется пунктиром «путь неизвестен».

    ⚠️ Последний участок периода не судим: вернулась машина или нет — ещё
    неизвестно, а объявить настоящее место подделкой хуже, чем оставить
    сомнительное.

    Возвращает (оставшиеся точки, список выброшенных участков).
    """
    from app.services import rc_service

    if len(points) < 3:
        return list(points), []

    # Режем на участки там, где путь между точками НЕ ИЗВЕСТЕН: либо переход
    # физически невозможен, либо машина надолго замолчала. Второе обязательно:
    # подменённый участок может закончиться просто молчанием, и тогда он
    # склеился бы с настоящим продолжением в один кусок.
    runs: list[list[tuple]] = [[points[0]]]
    impossible_before = [False]           # была ли граница слева НЕВОЗМОЖНОЙ
    for prev, cur in zip(points, points[1:]):
        impossible = _leg_impossible(prev, cur)
        if impossible or _leg_is_unknown(prev, cur):
            runs.append([cur])
            impossible_before.append(impossible)
        else:
            runs[-1].append(cur)
    if len(runs) < 3:
        return list(points), []

    kept: list[tuple] = []
    dropped: list[dict] = []
    for i, run in enumerate(runs):
        suspect = 0 < i < len(runs) - 1
        if suspect:
            before, after = runs[i - 1][-1], runs[i + 1][0]
            away_in = rc_service.haversine_m(before[1], before[2], run[0][1], run[0][2])
            away_out = rc_service.haversine_m(run[-1][1], run[-1][2], after[1], after[2])
            back = rc_service.haversine_m(before[1], before[2], after[1], after[2])
            left_bad, right_bad = impossible_before[i], impossible_before[i + 1]
            returned = back < max(away_in, away_out) / 2
            # Обе границы невозможны — это выброс любой длины (хоть одна точка).
            # Одна граница — верим только если участок реально далеко: так
            # выглядит подмена, а не погрешность.
            proven = (left_bad and right_bad) or (
                (left_bad or right_bad) and min(away_in, away_out) > UNREACHABLE_MIN_M
            )
            if proven and returned:
                dropped.append({
                    "from": run[0][0].isoformat(),
                    "to": run[-1][0].isoformat(),
                    "points": len(run),
                    "away_km": round(max(away_in, away_out) / 1000, 1),
                    "duration_label": duration_label(run[0][0], run[-1][0]),
                })
                continue
        kept.extend(run)
    return kept, dropped


def _leg_is_unknown(prev, cur) -> bool:
    """Между двумя точками потерялся путь — соединять их прямой нельзя.

    prev/cur — (observed_at, lat, lon, speed). Признаки:
      * прошло больше TRACK_LEG_GAP_SECONDS И машина сместилась заметно —
        точек за этот кусок не было, каким путём ехала, мы не знаем;
      * либо получившаяся скорость физически невозможна (скачок GPS).
    """
    from app.services import rc_service

    seconds = (cur[0] - prev[0]).total_seconds()
    if seconds <= 0:
        return False
    metres = rc_service.haversine_m(prev[1], prev[2], cur[1], cur[2])
    if metres <= TRACK_LEG_MAX_M:
        return False
    if seconds > TRACK_LEG_GAP_SECONDS:
        return True
    return (metres / seconds) * 3.6 > TRACK_LEG_MAX_KMH


def _push_move(
    out: list[dict], coords: list[list[float]], start, end,
    times: list | None = None,
) -> None:
    """Добавить кусок поездки, если в нём есть что рисовать.

    ⚠️ `times` — время КАЖДОЙ точки, по одному на каждую пару координат.
    Оно есть в базе (`VehicleTelemetryPoint.observed_at`) и обязано доезжать
    до карты: проигрыватель трека показывает часы и считает скорость по нему.
    Пока времени не было, плеер раскладывал точки равномерно внутри отрезка —
    и врал: стоянка проигрывалась так же быстро, как езда.
    """
    from app.services import rc_service

    if len(coords) < 2:
        return
    dist = sum(
        rc_service.haversine_m(
            coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1]
        )
        for i in range(1, len(coords))
    )
    out.append({
        "kind": "move",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "points": coords,
        "times": [t.isoformat() for t in (times or [])],
        "distance_km": round(dist / 1000, 2),
        "duration_label": duration_label(start, end),
    })


def build_track_segments(
    points: list[tuple[datetime, float, float, Decimal | float | int | None]],
    *,
    window_end: datetime,
) -> list[dict]:
    """Трек машины: отрезки «ехал / стоял / нет связи» с геометрией.

    points — [(observed_at, lat, lon, speed_kmh)] по возрастанию времени,
    уже отфильтрованные (только достоверные координаты).

    Возвращает список отрезков:
      * {"kind": "move",     "points": [[lat, lon], ...], "distance_km": ...}
      * {"kind": "stop",     "lat": ..., "lon": ..., "duration_label": "18 мин"}
      * {"kind": "nosignal", "points": [[lat, lon], [lat, lon]]}

    Почему разрыв связи — отдельный отрезок, а не просто линия: соединять две
    точки в разных концах города сплошной линией значит утверждать, что машина
    ехала именно так. Мы этого не знаем, поэтому рисуем пунктиром.
    """
    from app.services import rc_service

    pts = sorted(
        (( _utc(t), float(lat), float(lon), Decimal(str(s if s is not None else 0)))
         for t, lat, lon, s in points if t is not None),
        key=lambda p: p[0],
    )
    if not pts:
        return []

    # ⚠️ Выбросы («телепорт» метки) убираем ДО нарезки: иначе к ложной точке
    # тянется пунктир, и владелец читает его как «машина где-то там была».
    pts, _dropped = drop_unreachable_runs(pts)
    if not pts:
        return []

    segments = segment_movements(
        [(t, speed) for t, _, _, speed in pts], window_end=window_end
    )
    out: list[dict] = []
    for seg in segments:
        inside = [p for p in pts if seg["start"] <= p[0] <= seg["end"]]
        if seg["kind"] == "move":
            if len(inside) < 2:
                continue
            # Режем поездку там, где между точками потерялся кусок пути.
            # Иначе две далёкие точки соединяются прямой, и трек «едет через
            # дома» — владелец увидел это 22.08 на реальных данных.
            run: list[list[float]] = [[inside[0][1], inside[0][2]]]
            run_times = [inside[0][0]]
            run_start = inside[0][0]
            for prev, cur in zip(inside, inside[1:]):
                if _leg_is_unknown(prev, cur):
                    _push_move(out, run, run_start, prev[0], run_times)
                    out.append({
                        "kind": "nosignal",
                        "start": prev[0].isoformat(),
                        "end": cur[0].isoformat(),
                        "points": [[prev[1], prev[2]], [cur[1], cur[2]]],
                        "duration_label": duration_label(prev[0], cur[0]),
                        "reason": "no_points",
                    })
                    run = [[cur[1], cur[2]]]
                    run_times = [cur[0]]
                    run_start = cur[0]
                    continue
                run.append([cur[1], cur[2]])
                run_times.append(cur[0])
            _push_move(out, run, run_start, inside[-1][0], run_times)
        elif seg["kind"] == "stop":
            # Стоянку показываем одной точкой — последней известной внутри
            # отрезка (там машина и осталась стоять).
            anchor_pt = inside[-1] if inside else None
            if anchor_pt is None:
                continue
            out.append({
                "kind": "stop",
                "start": seg["start"].isoformat(),
                "end": seg["end"].isoformat(),
                "lat": anchor_pt[1],
                "lon": anchor_pt[2],
                "duration_label": duration_label(seg["start"], seg["end"]),
                "seconds": int((seg["end"] - seg["start"]).total_seconds()),
            })
        else:  # nosignal
            before = [p for p in pts if p[0] <= seg["start"]]
            after = [p for p in pts if p[0] >= seg["end"]]
            if not before or not after:
                continue
            out.append({
                "kind": "nosignal",
                "start": seg["start"].isoformat(),
                "end": seg["end"].isoformat(),
                "points": [
                    [before[-1][1], before[-1][2]],
                    [after[0][1], after[0][2]],
                ],
                "duration_label": duration_label(seg["start"], seg["end"]),
            })
    return out


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
