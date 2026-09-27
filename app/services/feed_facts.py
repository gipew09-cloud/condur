"""Карточка события в журнале приложения: то же, что владелец получает в
Telegram, только разложенное по строкам.

Владелец 26.09.2026, глядя на «Смена закрыта» в приложении: «некрасиво,
непонятно ничего» — одометр, пробег и GPS шли одной строкой через точки. И
дальше: «это не единственная некрасивая вещь или вещь, которой нету в
сообщениях журнала, которые приходят» — в Telegram про закрытие смены есть
рейсы, выручка, расходы, зарплата, сравнение с GPS и двигатель, а в журнале
было только число одометра.

Что здесь собирается для каждого события:
* `hero` — одно главное число крупно (пробег смены, сумма расхода);
* `facts` — строки «подпись — значение», у каждой может быть `tone`
  («warn» — обратить внимание, «bad» — плохо, «ok» — хорошо) и `hint` —
  пояснение мелким шрифтом;
* `photos` — снимки с подписями: у закрытой смены оба одометра, чтобы
  сравнить начало и конец рядом.

⚠️ Правило журнала прежнее: только записанное. Нет данных — нет строки;
ноль, которого мы не знаем, не пишем (PROBLEMS: выдуманное число хуже пустого
места — журнал открывают, чтобы разобраться в споре).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.services import finance_ledger, telemetry_service
from app.services.timeutil import fmt_dt, to_owner_tz

# Расхождение одометра и GPS, после которого строка подсвечивается — то же,
# что в уведомлении Telegram («больше 10 % — стоит проверить»).
_MISMATCH = telemetry_service.MILEAGE_MISMATCH_ALERT_RATIO


@dataclass
class ShiftTotals:
    """Итоги смены для карточки «Смена закрыта» — как в Telegram."""
    trips: int = 0
    revenue: Decimal = Decimal(0)
    pending_revenue: Decimal = Decimal(0)
    trips_without_revenue: int = 0
    expenses: Decimal = Decimal(0)


@dataclass
class Card:
    hero: dict | None = None
    facts: list[dict] = field(default_factory=list)
    photos: list[dict] = field(default_factory=list)

    def fact(self, label: str, value, *, tone: str | None = None, hint: str | None = None):
        if value is None or value == "":
            return
        row = {"label": label, "value": str(value)}
        if tone:
            row["tone"] = tone
        if hint:
            row["hint"] = hint
        self.facts.append(row)

    def photo(self, ref: str | None, caption: str | None):
        if ref:
            self.photos.append({"ref": ref, "caption": caption})

    def as_dict(self) -> dict:
        return {"hero": self.hero, "facts": self.facts, "photos": self.photos}


def _int(value) -> int | None:
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _km(value) -> str | None:
    number = _int(value)
    if not number:
        return None
    return f"{number:,}".replace(",", " ") + " км"


def _money(value) -> str | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return telemetry_service.rub_label(amount)


def _time(moment: datetime | None, tz_name: str | None) -> str | None:
    return fmt_dt(moment, tz_name, "%H:%M") if moment else None


def _span(start: datetime | None, end: datetime | None, tz_name: str | None) -> str | None:
    """«08:12 – 18:06 · 9 ч 54 мин»; другой день — с датой."""
    if start is None or end is None:
        return None
    a, b = to_owner_tz(start, tz_name), to_owner_tz(end, tz_name)
    left = a.strftime("%H:%M") if a.date() == b.date() else a.strftime("%d.%m %H:%M")
    return f"{left} – {b.strftime('%H:%M')} · {telemetry_service.duration_label(start, end)}"


def _route(trip) -> tuple[str | None, str | None]:
    if trip is None:
        return None, None
    return (trip.origin or "").strip() or None, (trip.destination or "").strip() or None


def _engine(payload: dict, tz_name: str | None, closing: bool) -> tuple[str | None, str | None]:
    """Двигатель на момент начала или конца смены — как строка «🔑» в Telegram."""
    on = payload.get("engine_on")
    if on is None:
        return None, None
    since_raw = payload.get("engine_since")
    since = None
    if isinstance(since_raw, str):
        try:
            since = datetime.fromisoformat(since_raw)
        except ValueError:
            since = None
    when = _time(since, tz_name)
    if on:
        value = f"работает с {when}" if when else "работает"
        # Смену закрыли, а мотор молотит — это стоит заметить.
        return value, ("warn" if closing else None)
    return (f"заглушен в {when}" if when else "заглушен"), None


def build(
    event_type: str,
    payload: dict | None,
    *,
    tz_name: str | None,
    expense=None,
    trip=None,
    zone=None,
    shift=None,
    totals: ShiftTotals | None = None,
    plates: dict | None = None,
) -> dict:
    p = payload or {}
    card = Card()

    if event_type == "shift_started":
        start = _int(shift.odometer_start) if shift is not None else _int(p.get("odometer_start"))
        card.fact("Одометр", _km(start))
        engine, tone = _engine(p, tz_name, closing=False)
        card.fact("Двигатель", engine, tone=tone)
        if shift is not None:
            card.photo(shift.odometer_start_photo_url, "Одометр в начале")

    elif event_type == "shift_completed":
        start = _int(shift.odometer_start) if shift is not None else None
        end = _int(shift.odometer_end) if shift is not None else None
        distance = _int(shift.distance_km if shift is not None else p.get("distance_km"))
        gps = _int(p.get("gps_km"))
        if distance:
            card.hero = {"value": _km(distance), "caption": "пробег по одометру"}
        elif gps:
            card.hero = {"value": _km(gps), "caption": "пробег по GPS — одометра ещё нет"}
        if start and end:
            card.fact("Одометр", f"{start:,} → {end:,} км".replace(",", " "))
        elif start:
            card.fact("Одометр в начале", _km(start))
        if gps and distance:
            diff = distance - gps
            big = abs(diff) / max(gps, 1) > _MISMATCH
            card.fact(
                "По GPS", _km(gps),
                tone="warn" if big else None,
                hint=(
                    f"на {abs(diff)} км {'меньше' if diff > 0 else 'больше'} одометра"
                    + (" — стоит проверить" if big else "")
                ),
            )
        if shift is not None:
            card.fact("Смена", _span(shift.started_at, shift.ended_at, tz_name))
        trips = totals.trips if totals is not None else _int(p.get("trips"))
        if trips is not None:
            card.fact("Рейсов", trips)
        if totals is not None:
            card.fact("Выручка", _money(totals.revenue))
            card.fact("Ждёт подтверждения", _money(totals.pending_revenue), tone="warn")
            if totals.trips_without_revenue:
                card.fact(
                    "Без выручки",
                    f"{totals.trips_without_revenue} из {totals.trips}",
                    tone="warn", hint="итог смены вырастет после ввода",
                )
            card.fact("Расходы", _money(totals.expenses))
        salary = _money(p.get("salary")) if distance else None
        # «≈» — как «приблизительно» в Telegram: точная — после расчёта месяца.
        card.fact("Зарплата", f"≈ {salary}" if salary else None)
        engine, tone = _engine(p, tz_name, closing=True)
        card.fact("Двигатель", engine, tone=tone)
        if shift is not None:
            # Оба снимка рядом: сравнить начало и конец можно одним взглядом.
            card.photo(shift.odometer_start_photo_url, "Начало смены")
            card.photo(shift.odometer_end_photo_url, "Конец смены")

    elif event_type in ("trip_created", "trip_in_transit", "trip_unloading",
                        "trip_completed", "trip_added_manual", "waybill_uploaded"):
        origin, destination = _route(trip)
        card.fact("Откуда", origin)
        card.fact("Куда", destination)
        if trip is not None:
            card.fact("Груз", (trip.cargo_name or "").strip() or None)
        if event_type == "trip_completed":
            card.fact("Топливо", _money(p.get("fuel_cost")))
            if trip is not None:
                card.fact("В рейсе", _span(trip.created_at, trip.completed_at, tz_name))
                card.fact(
                    "Выручка", _money(trip.revenue_rub) or "не указана",
                    tone=None if trip.revenue_rub else "warn",
                )
        if event_type == "trip_added_manual":
            card.fact("Пробег", "неизвестен", hint="рейс добавлен вручную, без GPS")
        if event_type == "waybill_uploaded":
            # Своя страница у каждой записи; у старых — из рейса (26.09.2026).
            ref = p.get("photo") if isinstance(p.get("photo"), str) else None
            card.photo(ref or (trip.waybill_photo_url if trip is not None else None), "ТТН")

    elif event_type in ("expense_submitted", "expense_approved", "expense_rejected",
                        "expense_amount_edited"):
        category = p.get("category") or (expense.category if expense is not None else None)
        amount = p.get("amount") if p.get("amount") is not None else p.get("new")
        if amount is None and expense is not None:
            amount = expense.amount_rub
        money = _money(amount)
        if money:
            card.hero = {
                "value": money,
                "caption": finance_ledger.category_label(category) if category else "расход",
            }
        old = p.get("old_amount") if p.get("old_amount") is not None else p.get("old")
        if event_type == "expense_amount_edited":
            card.fact("Было", _money(old))
            card.fact("Стало", money)
        if expense is not None:
            card.fact("Комментарий", (expense.description or "").strip() or None)
            card.fact("Оплата", finance_ledger.payment_label(getattr(expense, "payment_method", None)))
            status = {
                "pending": ("ждёт вашего решения", "warn"),
                "approved": ("одобрен", "ok"),
                "rejected": ("отклонён", "bad"),
            }.get(expense.status)
            if status:
                card.fact("Решение", status[0], tone=status[1])
            card.photo(expense.receipt_photo_url, "Чек")
        route = " → ".join(x for x in _route(trip) if x)
        card.fact("Рейс", route or None)

    elif event_type in ("rc_arrived", "rc_departed", "rc_downtime_alert",
                        "trip_rc_confirmed", "trip_rc_mismatch"):
        name = (zone.name if zone is not None else None) or p.get("rc_name")
        card.fact("РЦ", name)
        waited = _int(p.get("waited_minutes"))
        if waited:
            card.fact(
                "Стоял", telemetry_service.minutes_label(waited),
                tone="warn" if event_type == "rc_downtime_alert" else None,
            )
        card.fact("Простой к оплате", _money(p.get("suggested_amount_rub")), tone="warn")

    elif event_type == "silence_alert":
        minutes = _int(p.get("minutes_silent"))
        if minutes is None and p.get("hours_silent") is not None:
            try:
                minutes = int(round(float(p["hours_silent"]) * 60))
            except (TypeError, ValueError):
                minutes = None
        if minutes:
            card.hero = {"value": telemetry_service.minutes_label(minutes),
                         "caption": "без вестей от водителя"}
        card.fact("Что это", "смена открыта, но водитель ничего не отмечает",
                  hint="трекер при этом может слать точки — машина на карте видна")

    elif event_type == "no_show_alert":
        card.fact("Прошло", f"{p.get('hours')} ч" if p.get("hours") is not None else None,
                  tone="bad")
    elif event_type == "late_start_alert":
        card.fact("Ждали к", p.get("expected"), tone="warn")
    elif event_type == "fuel_overrun_alert":
        percent = p.get("percent")
        try:
            percent = round(float(percent)) if percent is not None else None
        except (TypeError, ValueError):
            percent = None
        card.fact("Сверх нормы", f"на {percent} %" if percent else None, tone="bad")
        card.fact("Лишнее топливо", _money(p.get("excess_rub")), tone="bad")
    elif event_type == "vehicle_mixup_alert":
        by_id = plates or {}
        card.fact("Стоит в смене", by_id.get(_int(p.get("shift_vehicle_id"))))
        card.fact("Едет без смены", by_id.get(_int(p.get("moving_vehicle_id"))), tone="warn")
    elif event_type in ("cash_submitted", "cash_confirmed"):
        money = _money(p.get("amount"))
        if money:
            card.hero = {"value": money, "caption": "наличные"}
    elif event_type in ("trip_revenue_set", "trip_revenue_approved", "trip_revenue_from_driver"):
        money = _money(p.get("revenue") if p.get("revenue") is not None else p.get("amount"))
        if money:
            card.hero = {"value": money, "caption": "выручка рейса"}
        route = " → ".join(x for x in _route(trip) if x)
        card.fact("Рейс", route or None)
    elif event_type == "odometer_edited":
        old = [p.get("old_start"), p.get("old_end")]
        new = [p.get("new_start"), p.get("new_end")]
        card.fact("Было", " → ".join(x for x in (_km(old[0]), _km(old[1])) if x) or None)
        card.fact("Стало", " → ".join(x for x in (_km(new[0]), _km(new[1])) if x) or None)
    elif event_type == "trip_route_edited":
        old, new = p.get("old") or {}, p.get("new") or {}
        def route_of(r):
            if not isinstance(r, dict):
                return None
            return " → ".join(x for x in (r.get("origin"), r.get("destination")) if x) or None
        card.fact("Было", route_of(old))
        card.fact("Стало", route_of(new))
    elif event_type == "driver_app_login":
        card.fact("Телефон", (p.get("device") or "").strip() or None)
    elif event_type == "sos":
        card.fact("Что", p.get("state"), tone="bad")
    elif event_type == "doc_expired_alert":
        card.fact("Документ", p.get("label"))
        expires = p.get("expires")
        try:
            when = date.fromisoformat(expires).strftime("%d.%m.%Y") if expires else None
        except ValueError:
            when = None
        card.fact("Истёк", when, tone="bad", hint="напоминаем один раз — продлите и впишите новый срок")
    elif event_type == "downtime":
        card.fact("Причина", p.get("label"), tone="warn")

    return card.as_dict()
