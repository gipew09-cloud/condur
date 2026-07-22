"""
Зажигание в сменах: переходы «завёл/заглушил двигатель», состояние на момент
открытия/закрытия смены и строка для уведомления владельцу.

Зачем: владелец хочет видеть, завели ли двигатель ДО начала смены (и за
сколько), и заглушили ли его к завершению. Датчик приходит по Wialon IPS
(ignition True/False); у EGTS-точек ignition чаще None — их пропускаем,
ничего не выдумываем.

Запуск: pytest tests/test_ignition.py
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

from decimal import Decimal  # noqa: E402

from app.services.telemetry_service import (  # noqa: E402
    engine_running_from_voltage,
    ignition_shift_line,
    ignition_state_at,
    ignition_transitions,
    wialon_odometer_km,
)


# ------------------------------------- пробег прибора из Wialon (метры → км)
def test_wialon_odometer_km_meters_to_km():
    """totalDistance в МЕТРАХ → км. 18.07: 1691610321 м = 1691610.32 км,
    ровно как одометр в самом Ставтрэке."""
    assert wialon_odometer_km(1691610321.7272892) == Decimal("1691610.3217272892")
    assert wialon_odometer_km(885937140.9424347) == Decimal("885937.1409424347")


def test_wialon_odometer_km_bad_values():
    assert wialon_odometer_km(None) is None
    assert wialon_odometer_km(0) is None
    assert wialon_odometer_km("мусор") is None

_T0 = datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc)


# ------------------------------------- зажигание по напряжению бортсети
def test_engine_running_from_voltage_24v():
    """Инцидент 18.07: борт 24 В. Двигатель заглушен → ~25 В (питание от АКБ),
    заведён → ~28 В (генератор). Совпадает с «Зажигание Off» Ставтрэка."""
    # реальные значения из логов: Т557 стоит заглушенный
    assert engine_running_from_voltage(25.36) is False
    assert engine_running_from_voltage(25.47) is False
    assert engine_running_from_voltage(26.38) is False   # У774 после остановки
    # реальные значения: У774 едет, генератор работает
    assert engine_running_from_voltage(28.09) is True
    assert engine_running_from_voltage(28.0) is True


def test_engine_running_from_voltage_12v():
    """Борт 12 В: заглушен ~12.6 В, заведён ~14 В."""
    assert engine_running_from_voltage(12.6) is False
    assert engine_running_from_voltage(12.9) is False
    assert engine_running_from_voltage(14.2) is True
    assert engine_running_from_voltage(13.8) is True


def test_engine_running_from_voltage_no_data():
    """Нет достоверного напряжения → None (решает сырой бит зажигания)."""
    assert engine_running_from_voltage(None) is None
    assert engine_running_from_voltage(0) is None        # питание борта потеряно
    assert engine_running_from_voltage(3.9) is None      # только АКБ терминала
    assert engine_running_from_voltage("мусор") is None


def _pt(minutes: float, ign):
    return (_T0 + timedelta(minutes=minutes), ign)


# ------------------------------------------------------------- переходы
def test_ignition_transitions_basic():
    """Выкл → вкл → выкл: два перехода, момент — первая точка нового состояния."""
    points = [_pt(0, False), _pt(5, False), _pt(10, True), _pt(15, True), _pt(20, False)]
    trs = ignition_transitions(points)
    assert [(t["on"], t["at"]) for t in trs] == [
        (True, _T0 + timedelta(minutes=10)),
        (False, _T0 + timedelta(minutes=20)),
    ]


def test_ignition_transitions_none_points_ignored():
    """EGTS-точки без датчика (None) не рождают событий и не рвут состояние."""
    points = [_pt(0, False), _pt(5, None), _pt(10, None), _pt(15, False), _pt(20, True)]
    trs = ignition_transitions(points)
    assert [(t["on"],) for t in trs] == [(True,)]
    assert ignition_transitions([_pt(0, None), _pt(5, None)]) == []
    assert ignition_transitions([]) == []


def test_ignition_transitions_long_flapping_does_not_crash():
    """Болтающийся контакт: датчик прыгает вкл/выкл КАЖДУЮ точку часами.
    Раньше склейка была рекурсивной — на такой серии упала бы по глубине
    рекурсии и уронила открытие смены. Теперь: не падает, дребезг склеен."""
    points = [_pt(m * 0.5, m % 2 == 0) for m in range(4000)]  # 33 часа дребезга
    trs = ignition_transitions(points)
    assert len(trs) <= 1  # вся серия склеивается, максимум один «хвост»

    # эталон: стабильные куски вокруг дребезга дают ровно один переход
    points = (
        [_pt(m, True) for m in range(0, 10)]
        + [_pt(10 + m * 0.5, m % 2 == 0) for m in range(6)]   # 3 мин дребезга
        + [_pt(15 + m, False) for m in range(10)]
    )
    trs = ignition_transitions(points)
    assert [(t["on"],) for t in trs] == [(False,)]


def test_ignition_transitions_flicker_squashed():
    """Одиночный кривой пакет (состояние держалось < 60 с между одинаковыми
    соседями) — дребезг, событий «завёл+заглушил» из него быть не должно."""
    points = [
        _pt(0, True), _pt(5, True),
        _pt(5.5, False),            # 30 секунд «выкл» — мусор
        _pt(6, True), _pt(10, True),
    ]
    assert ignition_transitions(points) == []
    # а честная короткая стоянка В КОНЦЕ данных (последнее состояние) остаётся
    points = [_pt(0, True), _pt(5, True), _pt(5.5, False)]
    trs = ignition_transitions(points)
    assert [(t["on"],) for t in trs] == [(False,)]


# ------------------------------------------- состояние на момент времени
def test_ignition_state_at_fresh_and_stale():
    """Свежая точка — состояние знаем; трекер молчит дольше 15 мин — None."""
    points = [_pt(0, False), _pt(10, True)]
    st = ignition_state_at(points, _T0 + timedelta(minutes=12))
    assert st == {"on": True, "since": _T0 + timedelta(minutes=10), "since_exact": True}
    # последняя точка на 10-й минуте, спрашиваем на 40-й → 30 мин молчания
    assert ignition_state_at(points, _T0 + timedelta(minutes=40)) is None
    assert ignition_state_at([], _T0) is None


def test_ignition_state_at_since_not_exact_when_window_starts_mid_state():
    """Окно началось, когда двигатель УЖЕ работал: честно говорим «не меньше»
    (since_exact=False), а не выдумываем точный момент запуска."""
    points = [_pt(0, True), _pt(5, True), _pt(10, True)]
    st = ignition_state_at(points, _T0 + timedelta(minutes=11))
    assert st["on"] is True
    assert st["since"] == _T0
    assert st["since_exact"] is False


def test_ignition_state_at_accepts_naive_datetimes():
    """SQLite/старые записи отдают naive datetime — не должно падать."""
    naive = [(t.replace(tzinfo=None), i) for t, i in [_pt(0, False), _pt(10, True)]]
    st = ignition_state_at(naive, (_T0 + timedelta(minutes=12)).replace(tzinfo=None))
    assert st is not None and st["on"] is True


def test_ignition_state_at_ignores_future_points():
    """Точки ПОЗЖЕ спрошенного момента не участвуют (дампы истории)."""
    points = [_pt(0, True), _pt(30, False)]
    st = ignition_state_at(points, _T0 + timedelta(minutes=5))
    assert st["on"] is True


# ------------------------------------------------- строка для владельца
_MSK = "Europe/Moscow"


def test_ignition_shift_line_start_engine_before_shift():
    """Завёл в 07:53 МСК, смену открыл в 08:00 МСК → «за 7 мин до начала»."""
    shift_open = _T0 + timedelta(minutes=7)  # 05:07 UTC = 08:07 МСК
    snap = {"on": True, "since": _T0, "since_exact": True}  # 05:00 UTC = 08:00 МСК
    line = ignition_shift_line(snap, moment=shift_open, tz_name=_MSK, closing=False)
    assert "работает" in line
    assert "08:00" in line              # время МСК, не UTC
    assert "за 7 мин до начала смены" in line


def test_ignition_shift_line_start_engine_running_long():
    snap = {"on": True, "since": _T0, "since_exact": False}
    line = ignition_shift_line(
        snap, moment=_T0 + timedelta(hours=2), tz_name=_MSK, closing=False
    )
    assert "не меньше 2 ч" in line


def test_ignition_shift_line_start_engine_off():
    snap = {"on": False, "since": _T0, "since_exact": True}
    line = ignition_shift_line(
        snap, moment=_T0 + timedelta(minutes=30), tz_name=_MSK, closing=False
    )
    assert "не заведён" in line and "08:00" in line


def test_ignition_shift_line_end_variants():
    # заглушил за 5 мин до завершения
    snap = {"on": False, "since": _T0, "since_exact": True}
    line = ignition_shift_line(
        snap, moment=_T0 + timedelta(minutes=5), tz_name=_MSK, closing=True
    )
    assert "заглушен" in line and "за 5 мин до завершения смены" in line
    # двигатель ещё работает на момент закрытия
    snap = {"on": True, "since": _T0, "since_exact": True}
    line = ignition_shift_line(
        snap, moment=_T0 + timedelta(minutes=5), tz_name=_MSK, closing=True
    )
    assert "ещё работает" in line


def test_ignition_shift_line_none_when_no_sensor():
    """Датчик не приходит → строки нет вовсе (не пишем «нет данных» в каждое
    уведомление машин без wialon-ретрансляции)."""
    assert ignition_shift_line(None, moment=_T0, tz_name=_MSK, closing=False) is None
    assert ignition_shift_line(None, moment=_T0, tz_name=_MSK, closing=True) is None


# --------------------------------------- выборка из БД (naive SQLite даты)
pytest.importorskip("aiosqlite")

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from app.models import Base, Owner, Vehicle, VehicleTelemetryPoint  # noqa: E402
from app.services.telemetry_service import (  # noqa: E402
    rc_presence_started_at,
    shift_ignition_snapshot,
)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


def test_shift_ignition_snapshot_reads_points():
    """Снимок состояния на момент открытия смены по реальным строкам БД:
    берёт только точки с датчиком (ignition NOT NULL) и до момента открытия."""
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            owner = Owner(telegram_id=1, full_name="Владелец")
            session.add(owner)
            await session.flush()
            vehicle = Vehicle(owner_id=owner.id, license_plate="Т557ОС178")
            session.add(vehicle)
            await session.flush()
            for minutes, ign, speed in ((0, False, 0), (5, None, 0), (10, True, 0)):
                session.add(VehicleTelemetryPoint(
                    owner_id=owner.id, vehicle_id=vehicle.id,
                    observed_at=_T0 + timedelta(minutes=minutes),
                    speed_kmh=Decimal(speed), ignition=ign, is_valid=True,
                ))
            await session.commit()

            snap = await shift_ignition_snapshot(
                session, vehicle_id=vehicle.id, moment=_T0 + timedelta(minutes=12)
            )
            assert snap is not None and snap["on"] is True
            assert snap["since_exact"] is True
            # у машины без датчика зажигания — честное None
            other = Vehicle(owner_id=owner.id, license_plate="Т772НХ178")
            session.add(other)
            await session.flush()
            assert await shift_ignition_snapshot(
                session, vehicle_id=other.id, moment=_T0
            ) is None
        await engine.dispose()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_rc_presence_started_at_reads_points():
    """Сквозная проверка: async-обёртка читает точки из БД, считает
    расстояние до РЦ и возвращает начало непрерывного пребывания —
    приехал в 06:28, а не «пару минут назад» в момент отъезда."""
    RC_LAT, RC_LON = 59.80, 30.40   # центр РЦ
    t0 = datetime(2026, 7, 19, 6, 28, tzinfo=timezone.utc)

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            owner = Owner(telegram_id=1, full_name="Владелец")
            session.add(owner)
            await session.flush()
            veh = Vehicle(owner_id=owner.id, license_plate="Т557ОС178")
            session.add(veh)
            await session.flush()
            # 70 минут внутри геозоны (почти в центре), затем выехал (далеко)
            for m in range(0, 71, 5):
                session.add(VehicleTelemetryPoint(
                    owner_id=owner.id, vehicle_id=veh.id,
                    observed_at=t0 + timedelta(minutes=m),
                    latitude=Decimal("59.8001"), longitude=Decimal("30.4001"),
                    speed_kmh=Decimal(0), is_valid=True,
                ))
            for m in range(72, 80, 2):
                session.add(VehicleTelemetryPoint(
                    owner_id=owner.id, vehicle_id=veh.id,
                    observed_at=t0 + timedelta(minutes=m),
                    latitude=Decimal("59.85"), longitude=Decimal("30.50"),  # ~7 км
                    speed_kmh=Decimal(40), is_valid=True,
                ))
            await session.commit()

            now = t0 + timedelta(minutes=80)
            fb = now - timedelta(minutes=3)   # «запасное» время (было бы 3 мин)
            start = await rc_presence_started_at(
                session, vehicle_id=veh.id, rc_lat=RC_LAT, rc_lon=RC_LON,
                exit_radius_m=600, now=now, fallback=fb,
            )
            # SQLite отдаёт наивную дату (в бою Postgres — с таймзоной); нормализуем
            start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            # начало пребывания — 06:28, а не запасные «3 минуты»
            assert start == t0
            assert (now - start).total_seconds() // 60 >= 70

            # нет точек внутри → берём запасное время
            far = await rc_presence_started_at(
                session, vehicle_id=veh.id, rc_lat=10.0, rc_lon=10.0,
                exit_radius_m=600, now=now, fallback=fb,
            )
            assert far == fb
        await engine.dispose()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
