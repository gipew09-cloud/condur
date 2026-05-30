"""
Модели = таблицы базы данных в виде Python-классов.
Каждый класс = одна таблица, каждый mapped_column = одна колонка.
Это ровно та же схема, что в schema.sql, но на языке SQLAlchemy 2.0.
"""
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Integer, String, Text, Boolean, Numeric, Date,
    DateTime, ForeignKey, Computed, CheckConstraint, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Общий "родитель" для всех моделей. От него наследуются все таблицы."""
    pass


# ========== ВЛАДЕЛЬЦЫ ==========
class Owner(Base):
    __tablename__ = "owners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    full_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(20))
    company_name: Mapped[str | None] = mapped_column(String(255))
    timezone: Mapped[str] = mapped_column(String(50), default="Europe/Moscow")
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ========== ВОДИТЕЛИ ==========
class Driver(Base):
    __tablename__ = "drivers"
    __table_args__ = (
        CheckConstraint(
            "salary_type IN ('per_km','per_trip','percent','fixed_per_shift')",
            name="ck_driver_salary_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    invite_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(20))

    salary_type: Mapped[str] = mapped_column(String(20), default="per_km")
    salary_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    per_diem_rub: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)

    # ожидаемое время начала смены, формат "HH:MM" в TZ владельца.
    # Используется APScheduler-ом, чтобы алёртить если водитель не начал.
    shift_start_time: Mapped[str | None] = mapped_column(String(5))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ========== МАШИНЫ ==========
class Vehicle(Base):
    __tablename__ = "vehicles"
    __table_args__ = (
        UniqueConstraint("owner_id", "license_plate", name="uq_vehicle_plate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    license_plate: Mapped[str] = mapped_column(String(20))
    brand: Mapped[str | None] = mapped_column(String(100))
    type: Mapped[str | None] = mapped_column(String(50))
    fuel_norm_per_100km: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))

    # документы — для алертов APScheduler за 30 дней до истечения
    osago_expires: Mapped[date | None] = mapped_column(Date)
    inspection_expires: Mapped[date | None] = mapped_column(Date)
    tacho_expires: Mapped[date | None] = mapped_column(Date)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ========== СМЕНЫ ==========
class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = (
        CheckConstraint("status IN ('started','completed','cancelled')", name="ck_shift_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id"))
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"))
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"))
    status: Mapped[str] = mapped_column(String(20), default="started")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    odometer_start: Mapped[int | None] = mapped_column(Integer)
    odometer_end: Mapped[int | None] = mapped_column(Integer)
    odometer_start_photo_url: Mapped[str | None] = mapped_column(Text)
    odometer_end_photo_url: Mapped[str | None] = mapped_column(Text)
    # вычисляемая колонка: Postgres сам посчитает пробег
    distance_km: Mapped[int | None] = mapped_column(
        Integer, Computed("odometer_end - odometer_start", persisted=True)
    )
    notes: Mapped[str | None] = mapped_column(Text)


# ========== РЕЙСЫ ==========
class Trip(Base):
    __tablename__ = "trips"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created','in_transit','unloading','completed','cancelled')",
            name="ck_trip_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id"), index=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id", ondelete="CASCADE"))
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"))
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"))
    status: Mapped[str] = mapped_column(String(20), default="created")

    origin: Mapped[str | None] = mapped_column(Text)
    destination: Mapped[str | None] = mapped_column(Text)
    cargo_name: Mapped[str | None] = mapped_column(Text)

    waybill_number: Mapped[str | None] = mapped_column(String(100))
    waybill_photo_url: Mapped[str | None] = mapped_column(Text)

    revenue_rub: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    fuel_cost_rub: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    other_costs_rub: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    # вычисляемая прибыль = выручка - топливо - прочие расходы
    profit_rub: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        Computed(
            "COALESCE(revenue_rub,0) - COALESCE(fuel_cost_rub,0) - COALESCE(other_costs_rub,0)",
            persisted=True,
        ),
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ========== РАСХОДЫ ==========
class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint(
            "category IN ('fuel','repair','parking','fine','toll','other')", name="ck_expense_category"
        ),
        CheckConstraint("status IN ('pending','approved','rejected')", name="ck_expense_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id"), index=True)
    trip_id: Mapped[int | None] = mapped_column(ForeignKey("trips.id"))
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"))
    driver_id: Mapped[int] = mapped_column(ForeignKey("drivers.id"))
    category: Mapped[str] = mapped_column(String(20))
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    receipt_photo_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ========== СОБЫТИЯ (лог всех действий) ==========
class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id"), index=True)
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("drivers.id"))
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"))
    trip_id: Mapped[int | None] = mapped_column(ForeignKey("trips.id"))
    event_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ========== ШАБЛОНЫ МАРШРУТОВ ==========
class RouteTemplate(Base):
    """
    Предзаданные маршруты владельца. Водитель при создании рейса
    может выбрать один из шаблонов вместо ручного ввода городов и груза.
    """
    __tablename__ = "route_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    origin: Mapped[str] = mapped_column(Text)
    destination: Mapped[str] = mapped_column(Text)
    default_cargo: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ========== РУЧНЫЕ ДОХОДЫ И РАСХОДЫ ВЛАДЕЛЬЦА ==========
class ManualEntry(Base):
    """
    Произвольные финансовые записи владельца, не привязанные к рейсам:
    например аренда офиса, лизинг, аванс водителю, нерейсовая выручка.
    Используется на странице /finances.
    """
    __tablename__ = "manual_entries"
    __table_args__ = (
        CheckConstraint("type IN ('income','expense')", name="ck_manual_entry_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(10))
    category: Mapped[str | None] = mapped_column(String(100))
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    description: Mapped[str | None] = mapped_column(Text)
    entry_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ========== ПОДПИСКИ / ТАРИФЫ ==========
class Subscription(Base):
    """
    Тариф владельца. Один owner = одна активная запись.
    Лимит машин проверяется при добавлении новой машины.
    """
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "plan IN ('free','base','business','pro')", name="ck_subscription_plan"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("owners.id", ondelete="CASCADE"), unique=True, index=True
    )
    plan: Mapped[str] = mapped_column(String(20), default="free")
    vehicles_limit: Mapped[int] = mapped_column(Integer, default=2)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ========== КЭШ ДНЕВНОЙ СВОДКИ ==========
class DailySummary(Base):
    __tablename__ = "daily_summaries"
    __table_args__ = (
        UniqueConstraint("owner_id", "date", name="uq_summary_owner_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("owners.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    total_trips: Mapped[int] = mapped_column(Integer, default=0)
    total_km: Mapped[int] = mapped_column(Integer, default=0)
    total_revenue: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    total_fuel_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
