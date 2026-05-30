"""
Расчёт зарплаты водителя по итогам смены.

Формулы:
  per_km          : ставка × пробег + per_diem
  per_trip        : ставка × число завершённых рейсов + per_diem
  percent         : % × сумма выручки + per_diem
  fixed_per_shift : фиксированная ставка + per_diem

per_diem — суточные × число календарных дней, на которые пришлась смена.
Например, смена 30 мая 22:00 → 31 мая 06:00 — это 2 дня.

Важно: считаем в Decimal, а не float. На больших суммах float даёт
накопленные ошибки округления (классическая бухгалтерская беда),
а нам деньги выдавать людям.
"""
from decimal import Decimal

from app.models import Driver, Shift, Trip


def _count_days(shift: Shift) -> int:
    if shift.started_at is None or shift.ended_at is None:
        return 1
    days = (shift.ended_at.date() - shift.started_at.date()).days + 1
    return max(1, days)


def _completed_trips(trips: list[Trip]) -> list[Trip]:
    return [t for t in trips if t.status == "completed"]


def calculate_salary(driver: Driver, shift: Shift, trips: list[Trip]) -> Decimal:
    rate = driver.salary_rate or Decimal(0)
    completed = _completed_trips(trips)

    if driver.salary_type == "per_km":
        base = Decimal(shift.distance_km or 0) * rate
    elif driver.salary_type == "per_trip":
        base = Decimal(len(completed)) * rate
    elif driver.salary_type == "percent":
        total_revenue = sum((t.revenue_rub or Decimal(0)) for t in completed) or Decimal(0)
        base = (total_revenue * rate) / Decimal(100)
    else:  # fixed_per_shift
        base = rate

    per_diem = (driver.per_diem_rub or Decimal(0)) * Decimal(_count_days(shift))
    total = base + per_diem
    return total.quantize(Decimal("0.01"))
