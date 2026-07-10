"""
Преобразование времени в таймзону владельца.

В БД всё хранится в UTC (TIMESTAMPTZ + datetime.now(timezone.utc)).
Здесь конвертируем в локальное время владельца — для вывода в боте
и в веб-кабинете.

Python 3.9+ имеет zoneinfo в стандартной библиотеке, pytz не нужен.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import settings

RU_MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн",
                   "июл", "авг", "сен", "окт", "ноя", "дек"]


def owner_tz(timezone_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or settings.default_timezone)
    except Exception:
        return ZoneInfo(settings.default_timezone)


def to_owner_tz(dt: datetime | None, timezone_name: str | None) -> datetime | None:
    """UTC-aware datetime → таймзона владельца. None прокидывает как None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(owner_tz(timezone_name))


def fmt_dt(dt: datetime | None, timezone_name: str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    local = to_owner_tz(dt, timezone_name)
    return local.strftime(fmt) if local else "—"


def fmt_time(dt: datetime | None, timezone_name: str | None) -> str:
    return fmt_dt(dt, timezone_name, "%H:%M")


def now_in_tz(timezone_name: str | None) -> datetime:
    return datetime.now(owner_tz(timezone_name))


def month_floor(d: date) -> date:
    return date(d.year, d.month, 1)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def cashflow_buckets(df: date, dt: date):
    """Шаг графика «Денежный поток» по длине периода: ≤45 дней — дни,
    ≤200 — недели (с понедельника), дальше — месяцы («июл 2026», а не
    «июл 26» — владелец читал «26» как число месяца).
    Возвращает (labels, bucket_keys, key_of)."""
    span = (dt - df).days + 1
    if span <= 45:
        keys = [df + timedelta(days=i) for i in range(span)]
        labels = [k.strftime("%d.%m") for k in keys]

        def key_of(d):
            return d

        return labels, keys, key_of
    if span <= 200:
        first = df - timedelta(days=df.weekday())  # понедельник первой недели
        keys = []
        k = first
        while k <= dt:
            keys.append(k)
            k += timedelta(days=7)
        labels = [f"нед. {k:%d.%m}" for k in keys]

        def key_of(d):
            return d - timedelta(days=d.weekday())

        return labels, keys, key_of
    first = month_floor(df)
    keys = []
    k = first
    while k <= dt:
        keys.append(k)
        k = add_months(k, 1)
    labels = [f"{RU_MONTHS_SHORT[k.month - 1]} {k:%Y}" for k in keys]
    bucket_keys = [(k.year, k.month) for k in keys]

    def key_of(d):
        return (d.year, d.month)

    return labels, bucket_keys, key_of


def smart_since_label(dt: datetime | None, timezone_name: str | None) -> str:
    """«с 21:52» (сегодня) / «со вчера, 21:52» / «с 01.07, 21:52» (раньше).

    Для карты: когда машина стоит с прошлых суток, одного времени мало —
    видно должно быть и число.
    """
    local = to_owner_tz(dt, timezone_name)
    if local is None:
        return "—"
    today = now_in_tz(timezone_name).date()
    if local.date() == today:
        return f"с {local:%H:%M}"
    days = (today - local.date()).days
    if days == 1:
        return f"со вчера, {local:%H:%M}"
    return f"с {local:%d.%m}, {local:%H:%M}"
