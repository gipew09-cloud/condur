"""
Расчёт зарплаты водителя по итогам смены.

Формулы:
  per_km          : ставка × пробег + per_diem
  per_trip        : ставка × число завершённых рейсов + per_diem
  percent         : % × сумма выручки + per_diem
  fixed_per_shift : фиксированная ставка + per_diem
  fixed_per_month : помесячный оклад — за смену НЕ начисляется (0), платится
                    отдельно раз в месяц; ставка хранится как сумма оклада.

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


def estimate_trip_salary(
    driver: Driver,
    trip: Trip,
    shift_distance_km: int | None,
    shift_completed_trips: int,
) -> Decimal:
    """
    Оценка зарплаты водителя ЗА ОДИН РЕЙС. Используется в карточке рентабельности.

    Точно посчитать «сколько именно водитель заработает на этом рейсе» нельзя:
    ЗП считается за смену целиком. Здесь даём разумную оценку:
      per_km          : пробег_смены / число_рейсов × ставка_км
      per_trip        : ставка за рейс
      percent         : выручка_рейса × процент / 100
      fixed_per_shift : фикс / число_рейсов
    """
    rate = driver.salary_rate or Decimal(0)
    trips_in_shift = max(1, shift_completed_trips)

    if driver.salary_type == "per_km":
        if shift_distance_km:
            per_trip_km = Decimal(shift_distance_km) / Decimal(trips_in_shift)
            return (per_trip_km * rate).quantize(Decimal("0.01"))
        return Decimal(0)
    if driver.salary_type == "per_trip":
        return rate.quantize(Decimal("0.01"))
    if driver.salary_type == "percent":
        return ((trip.revenue_rub or Decimal(0)) * rate / Decimal(100)).quantize(Decimal("0.01"))
    if driver.salary_type == "fixed_per_month":
        return Decimal(0)  # помесячный оклад не привязан к рейсу
    # fixed_per_shift
    return (rate / Decimal(trips_in_shift)).quantize(Decimal("0.01"))


def calculate_salary(driver: Driver, shift: Shift, trips: list[Trip]) -> Decimal:
    rate = driver.salary_rate or Decimal(0)
    completed = _completed_trips(trips)

    # Помесячный оклад за смену не начисляется (платится раз в месяц отдельно).
    if driver.salary_type == "fixed_per_month":
        return Decimal(0)

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
