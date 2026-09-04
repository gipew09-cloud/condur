"""Произвольный период трека и список смен для вкладки «Треки».

Владелец 27.08.2026: «треки недоделаны», нужен плеер и произвольный период
«с… по…». Раньше трек умел только «последние N часов» — рабочий день за
прошлый вторник посмотреть было нельзя.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

pytest.importorskip("aiogram")
pytest.importorskip("aiosqlite")

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from app.models import Base, Driver, Owner, Shift, Trip, Vehicle  # noqa: E402
from app.web.router import _period_bounds, api_vehicle_shifts  # noqa: E402

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


def test_period_is_read_in_the_owners_timezone():
    """⚠️ «26.08 00:00» значит полночь У ВЛАДЕЛЬЦА, а не в Гринвиче.

    Раньше наивное время из поля браузера читалось как UTC, и период уезжал на
    три часа: владелец 30.08.2026 заметил это как «трек должен заканчиваться
    там, где последняя точка до 12 часов по московскому времени».
    """
    since, until = _period_bounds(
        "2026-08-20T07:00", "2026-08-20T20:00", 12, NOW, "Europe/Moscow",
    )
    # 07:00 в Москве — это 04:00 UTC
    assert since.astimezone(timezone.utc).hour == 4
    assert until.astimezone(timezone.utc).hour == 17

    # у владельца в другом поясе тот же ввод даёт другой момент
    vlad, _ = _period_bounds(
        "2026-08-20T07:00", "2026-08-20T20:00", 12, NOW, "Asia/Vladivostok",
    )
    assert vlad.astimezone(timezone.utc).hour == 21
    assert vlad < since


def test_period_survives_swapped_ends():
    """Владелец выбрал «с 20:00 по 07:00» — не пустой ответ, а тот же день."""
    since, until = _period_bounds(
        "2026-08-20T20:00", "2026-08-20T07:00", 12, NOW, "Europe/Moscow",
    )
    assert since.hour == 7 and until.hour == 20


def test_period_falls_back_to_hours():
    since, until = _period_bounds(None, None, 6, NOW)
    assert until == NOW
    assert NOW - since == timedelta(hours=6)


def test_period_is_capped_at_a_month():
    """Трек за год — миллионы точек, браузер владельца ляжет.

    Отдаём последний месяц периода, а не пустой ответ: пустой экран читается
    как «данных нет», и владелец будет искать несуществующую поломку.
    """
    since, until = _period_bounds(
        "2025-08-20T00:00", "2026-08-20T00:00", 12, NOW, "Europe/Moscow",
    )
    assert until - since == timedelta(days=31)
    assert until.hour == 0 and until.day == 20


def test_broken_dates_do_not_break_the_track():
    since, until = _period_bounds("вчера", "сегодня", 3, NOW, "Europe/Moscow")
    assert NOW - since == timedelta(hours=3)


def _run(scenario):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        loop.close()


async def _db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    session = maker()
    owner = Owner(telegram_id=1, full_name="Владелец", timezone="Europe/Moscow")
    session.add(owner)
    await session.flush()
    vehicle = Vehicle(owner_id=owner.id, license_plate="Т557ОС178", is_active=True)
    driver = Driver(owner_id=owner.id, full_name="Саломов", telegram_id=2)
    session.add_all([vehicle, driver])
    await session.flush()
    return session, owner, vehicle, driver


def test_shifts_carry_route_and_odometer_mileage():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        shift = Shift(
            owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
            started_at=datetime(2026, 8, 20, 4, 12, tzinfo=timezone.utc),
            ended_at=datetime(2026, 8, 20, 16, 47, tzinfo=timezone.utc),
            odometer_start=100000, odometer_end=100214,
        )
        session.add(shift)
        await session.flush()
        session.add_all([
            Trip(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                 shift_id=shift.id, origin="склад 5.18", destination="РЦ 7 шагов"),
            Trip(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                 shift_id=shift.id, origin="РЦ 7 шагов", destination="база"),
        ])
        await session.commit()

        data = await api_vehicle_shifts(
            vehicle.id, owner, session,
            frm="2026-08-20T00:00", to="2026-08-21T00:00",
        )
        row = data["shifts"][0]
        assert row["route"] == "склад 5.18 → РЦ 7 шагов → база"
        assert row["trips"] == 2
        assert row["driver"] == "Саломов"
        assert row["is_open"] is False
        await session.close()
    _run(scenario)


def test_open_shift_shows_up_and_mileage_is_not_faked():
    """Открытая смена попадает в список, а пробег без одометра — не ноль."""
    async def scenario():
        session, owner, vehicle, driver = await _db()
        session.add(Shift(
            owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
            started_at=datetime(2026, 8, 30, 5, 0, tzinfo=timezone.utc),
            ended_at=None, odometer_start=None, odometer_end=None,
        ))
        await session.commit()

        data = await api_vehicle_shifts(
            vehicle.id, owner, session,
            frm="2026-08-30T00:00", to="2026-08-30T23:59",
        )
        row = data["shifts"][0]
        assert row["is_open"] is True
        assert row["ended_label"] is None
        # ⚠️ ноль читался бы как «никуда не ездил»
        assert row["odometer_distance_km"] is None
        assert row["route"] is None
        await session.close()
    _run(scenario)


def test_shifts_of_another_owner_are_not_returned():
    async def scenario():
        session, owner, vehicle, driver = await _db()
        stranger = Owner(telegram_id=99, full_name="Чужой")
        session.add(stranger)
        await session.flush()
        session.add(Shift(
            owner_id=stranger.id, driver_id=driver.id, vehicle_id=vehicle.id,
            started_at=datetime(2026, 8, 30, 5, 0, tzinfo=timezone.utc),
        ))
        await session.commit()

        data = await api_vehicle_shifts(
            vehicle.id, owner, session,
            frm="2026-08-30T00:00", to="2026-08-30T23:59",
        )
        assert data["shifts"] == []
        await session.close()
    _run(scenario)


def test_every_point_carries_its_own_time():
    """Время КАЖДОЙ точки доезжает до карты.

    Владелец 30.08.2026: «почему сервер не хранит время каждой отдельной точки
    — это очень плохо». Он прав в претензии, но время в базе есть
    (`VehicleTelemetryPoint.observed_at`) — его просто не отдавали. Без него
    плеер раскладывал точки равномерно внутри отрезка, и стоянка на светофоре
    проигрывалась так же быстро, как езда по трассе.
    """
    from decimal import Decimal

    from app.services.telemetry_service import build_track_segments

    start = datetime(2026, 8, 26, 9, 42, tzinfo=timezone.utc)
    points = [
        (start + timedelta(minutes=2 * i), 59.93 + i * 0.002, 30.33 + i * 0.002, Decimal("40"))
        for i in range(8)
    ]
    moves = [
        seg for seg in build_track_segments(points, window_end=start + timedelta(minutes=20))
        if seg["kind"] == "move"
    ]
    assert moves, "поездка не собралась"
    seg = moves[0]
    assert len(seg["times"]) == len(seg["points"])
    assert seg["times"][0] == points[0][0].isoformat()
    assert seg["times"][-1] == points[-1][0].isoformat()


# ---------------------------------------------------------------------------
# Качество данных за период: честный ответ вместо пустого экрана
# ---------------------------------------------------------------------------
# Владелец 30.08.2026: «за выбранный период должны быть видны и пробег по
# одометру, и GPS-пробег; а если данных нет — так и написано». Пустой экран он
# читает как поломку программы.

def test_names_say_where_the_kilometres_came_from():
    """⚠️ Одно имя `distance_km` значило РАЗНОЕ в двух эндпоинтах.

    В треке это был путь по GPS, в сменах — показания одометра. Рано или поздно
    интерфейс показал бы одно вместо другого, а владелец предъявил бы это
    водителю. Имя обязано называть источник.
    """
    import inspect

    from app.web import router

    track = inspect.getsource(router.api_vehicle_track)
    shifts = inspect.getsource(router.api_vehicle_shifts)
    assert '"gps_path_distance_km"' in track
    assert '"distance_km": round(' not in track
    assert '"odometer_distance_km"' in shifts
    assert '"distance_km": shift.distance_km' not in shifts


def test_no_points_is_said_out_loud():
    quality = router_quality([], [], NOW - timedelta(hours=2), NOW)
    assert quality["gps"]["status"] == "missing"
    assert quality["gps"]["reason"] == "no_points_in_period"
    assert quality["odometer"]["status"] == "missing"


def test_gap_at_the_edges_is_reported():
    """Молчание до первой точки и после последней — тоже пропуск."""
    start = NOW - timedelta(hours=3)
    rows = [
        (start + timedelta(hours=1), 59.93, 30.33, 40, None),
        (start + timedelta(hours=1, minutes=1), 59.94, 30.34, 40, None),
    ]
    quality = router_quality(rows, [], start, NOW)
    reasons = {g["reason"] for g in quality["gps"]["gaps"]}
    assert "before_first_point" in reasons
    assert "after_last_point" in reasons
    assert quality["gps"]["status"] == "partial"


def test_future_is_not_a_gap():
    """Конец периода в будущем не должен превращаться в «нет сигнала»."""
    rows = [
        (NOW - timedelta(minutes=2), 59.93, 30.33, 40, None),
        (NOW - timedelta(minutes=1), 59.94, 30.34, 40, None),
    ]
    quality = router_quality(rows, [], NOW - timedelta(minutes=10), NOW + timedelta(days=1))
    assert not [g for g in quality["gps"]["gaps"] if g["reason"] == "after_last_point"]


def test_odometer_never_shows_zero_instead_of_nothing():
    """⚠️ Ноль читается как «никуда не ездил». Это не то же, что «нет данных»."""
    rows = [(NOW, 59.93, 30.33, 0, None), (NOW, 59.93, 30.33, 0, None)]
    odo = router_quality(rows, [], NOW - timedelta(hours=1), NOW)["odometer"]
    assert odo["status"] == "missing"
    assert "odometer_distance_km" not in odo


def test_odometer_going_backwards_is_not_glued_together():
    """Сброс прибора или замена трекера — складывать такие показания нельзя."""
    rows = [
        (NOW - timedelta(hours=1), 59.93, 30.33, 40, 958_700),
        (NOW, 59.94, 30.34, 40, 120),
    ]
    odo = router_quality(rows, [], NOW - timedelta(hours=2), NOW)["odometer"]
    assert odo["status"] == "reset_detected"
    assert odo.get("odometer_distance_km") is None


def test_odometer_pair_gives_the_distance():
    rows = [
        (NOW - timedelta(hours=1), 59.93, 30.33, 40, 958_700.0),
        (NOW, 59.94, 30.34, 40, 958_731.0),
    ]
    odo = router_quality(rows, [], NOW - timedelta(hours=2), NOW)["odometer"]
    assert odo["status"] == "ok"
    assert odo["odometer_distance_km"] == 31.0


def router_quality(rows, segments, since, until):
    from app.web.router import _period_quality
    return _period_quality(rows, segments, since, until, NOW)


def test_every_track_point_carries_instruments():
    """У точки трека есть приборы на ту же минуту.

    Владелец 30.08.2026: «трек — это профессиональный уровень, это не только
    куда машина ехала; какая температура была в эту минуту».
    """
    from types import SimpleNamespace

    from app.web.router import _with_instruments

    t0 = datetime(2026, 8, 26, 9, 42, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=1)
    # Порядок колонок — как в запросе трека: время, широта, долгота, скорость,
    # пробег прибора, сырое топливо, температура топлива, зажигание, напряжение.
    rows = [
        (t0, 59.93, 30.33, 40, 958_700, 512, 17.0, True, 27.4),
        (t1, 59.94, 30.34, 42, 958_701, None, None, False, None),
    ]
    segments = [{
        "kind": "move",
        "points": [[59.93, 30.33], [59.94, 30.34]],
        "times": [t0.isoformat(), t1.isoformat()],
    }]
    vehicle = SimpleNamespace(fuel_calibration=None)
    out = _with_instruments(segments, rows, vehicle)[0]

    assert len(out["fuel_temp_c"]) == len(out["points"]) == 2
    assert out["fuel_temp_c"] == [17.0, None]
    assert out["ignition"] == [True, False]
    # ⚠️ Без тарировки бака литров быть не может — сырое значение датчика
    # литрами не является, это выдуманное число.
    assert out["fuel_litres"] == [None, None]
    # Скорость и напряжение — такие же приборы на ту же минуту. Владелец
    # 04.09.2026: «нету датчика напряжения» — в карточке машины он был, а в
    # треке его не было, и вопрос «когда машину обесточили» оставался без ответа.
    assert out["speed_kmh"] == [40.0, 42.0]
    assert out["voltage"] == [27.4, None]


def test_stop_segments_are_left_alone():
    """У стоянки нет массива времён — трогать её нечем и не нужно."""
    from types import SimpleNamespace

    from app.web.router import _with_instruments

    segments = [{"kind": "stop", "lat": 59.9, "lon": 30.3, "duration_label": "18 мин"}]
    out = _with_instruments(segments, [], SimpleNamespace(fuel_calibration=None))[0]
    assert "fuel_litres" not in out


def test_events_belong_to_the_vehicle_not_to_the_driver():
    """Событие привязывается к машине через смену или рейс, а не по водителю.

    Владелец 30.08.2026 хочет видеть на треке, где водитель нажал кнопку и где
    сдал груз. ⚠️ Связи «событие → машина» в таблице `events` нет. Определять её
    «водитель в этот день ездил на этой машине» нельзя: за смену он может
    пересесть, и события уедут на чужой трек.
    """
    from app.models import Event
    from app.web.router import api_vehicle_events

    async def scenario():
        session, owner, vehicle, driver = await _db()
        other = Vehicle(owner_id=owner.id, license_plate="Т772НХ178", is_active=True)
        session.add(other)
        await session.flush()

        mine = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                     started_at=NOW - timedelta(hours=5))
        alien = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=other.id,
                      started_at=NOW - timedelta(hours=5))
        session.add_all([mine, alien])
        await session.flush()
        session.add_all([
            Event(owner_id=owner.id, driver_id=driver.id, shift_id=mine.id,
                  event_type="shift_started", created_at=NOW - timedelta(hours=4)),
            Event(owner_id=owner.id, driver_id=driver.id, shift_id=alien.id,
                  event_type="waybill_uploaded", created_at=NOW - timedelta(hours=3)),
            # служебная рассылка — на карте ей не место
            Event(owner_id=owner.id, driver_id=driver.id, shift_id=mine.id,
                  event_type="weekly_review_sent", created_at=NOW - timedelta(hours=2)),
        ])
        await session.commit()

        data = await api_vehicle_events(
            vehicle.id, owner, session,
            frm=(NOW - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M"),
            to=NOW.strftime("%Y-%m-%dT%H:%M"),
        )
        kinds = [e["type"] for e in data["events"]]
        assert kinds == ["shift_started"], kinds
        assert data["events"][0]["label"] == "Смена начата"
        await session.close()
    _run(scenario)


def test_events_carry_no_coordinates():
    """⚠️ Сервер НЕ придумывает место события.

    Координату считает карта, сопоставляя время события с точками трека. Если
    точек в эту минуту нет, событие останется в списке без места — а не встанет
    посреди пунктирного участка, где путь неизвестен.
    """
    from app.models import Event
    from app.web.router import api_vehicle_events

    async def scenario():
        session, owner, vehicle, driver = await _db()
        shift = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                      started_at=NOW - timedelta(hours=2))
        session.add(shift)
        await session.flush()
        session.add(Event(owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
                          event_type="sos", created_at=NOW - timedelta(hours=1)))
        await session.commit()

        data = await api_vehicle_events(
            vehicle.id, owner, session,
            frm=(NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M"),
            to=NOW.strftime("%Y-%m-%dT%H:%M"),
        )
        event = data["events"][0]
        assert event["label"] == "SOS"
        assert "lat" not in event and "lon" not in event
        await session.close()
    _run(scenario)


def test_departure_is_the_first_movement_not_the_shift_start():
    """Просмотр начинается там, где машина ТРОНУЛАСЬ.

    Владелец 31.08.2026: «почему трек записался только когда он начал смену, а
    не когда сдвинулся». Смену открывают кнопкой, а машина до этого стоит.
    """
    from app.web.router import _departure_at

    segments = [
        {"kind": "stop", "lat": 59.9, "lon": 30.3, "duration_label": "1 ч 12 мин"},
        {"kind": "move", "start": "2026-08-26T07:42:00+00:00", "points": [], "times": []},
        {"kind": "stop", "lat": 59.95, "lon": 30.35, "duration_label": "20 мин"},
        {"kind": "move", "start": "2026-08-26T10:11:00+00:00", "points": [], "times": []},
    ]
    assert _departure_at(segments) == "2026-08-26T07:42:00+00:00"


def test_departure_is_none_when_the_vehicle_never_moved():
    """Не ехала вовсе — подсказки нет, а не «начните с нуля»."""
    from app.web.router import _departure_at

    assert _departure_at([{"kind": "stop", "lat": 59.9, "lon": 30.3}]) is None
    assert _departure_at([]) is None


# ------------------------------------------------------ «телепорт» метки
def test_gps_spike_is_dropped_but_a_real_point_after_a_gap_is_kept():
    """Точка, куда машина улететь не могла и тут же вернуться, — выброс.

    Владелец 04.09.2026: «точка телепортирует на секунду в какое-то другое
    место и обратно на машину». Такие точки трекер отдаёт с признаком
    «координаты достоверны»: у аэропорта и военных объектов сигнал глушат.

    ⚠️ Односторонний скачок выбрасывать нельзя: так выглядит честная точка
    после потери связи. Там путь неизвестен — рисуется пунктир, а не пустота.
    """
    from app.services import telemetry_service

    t0 = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    lat, lon = 59.85, 30.42
    points = [(t0 + timedelta(seconds=40 * i), lat + i * 0.004, lon, 40)
              for i in range(6)]
    # выброс между 3-й и 4-й точкой: 11 км в сторону и назад за 20 секунд
    points.append((t0 + timedelta(seconds=40 * 3 + 20), lat + 0.112, lon + 0.1, 42))
    points.sort(key=lambda p: p[0])

    clean, dropped = telemetry_service.drop_gps_spikes(points)
    assert dropped == 1
    assert all(abs(p[1] - (lat + 0.112)) > 1e-9 for p in clean)

    # честная точка после минутного молчания остаётся на месте
    after_gap = [
        (t0, 59.85, 30.42, 40),
        (t0 + timedelta(minutes=1), 59.95, 30.52, 40),
        (t0 + timedelta(minutes=1, seconds=40), 59.951, 30.521, 40),
    ]
    assert telemetry_service.drop_gps_spikes(after_gap)[1] == 0


def test_track_has_no_dashed_excursion_to_a_spike():
    """Выброс не должен оставлять на карте пунктир «машина была там».

    Пунктир к ложной точке хуже её самой: он утверждает, что машина туда
    ездила, — а это и есть то, что владелец предъявляет водителю.
    """
    from app.services import telemetry_service

    t0 = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    lat, lon = 59.85, 30.42
    points = [(t0 + timedelta(seconds=40 * i), lat + i * 0.004, lon, 40)
              for i in range(12)]
    points.append((t0 + timedelta(seconds=40 * 8 + 20), lat + 0.132, lon + 0.1, 42))
    points.sort(key=lambda p: p[0])

    segments = telemetry_service.build_track_segments(
        points, window_end=t0 + timedelta(minutes=20)
    )
    assert [s["kind"] for s in segments] == ["move"]
    assert len(segments[0]["points"]) == 12


def test_receiver_marks_the_jump_instead_of_moving_the_car():
    """Приёмник помечает скачок недостоверным — метка на карте не прыгает.

    ⚠️ Судим только по соседним во времени точкам: если трекер молчал час,
    машина могла честно уехать далеко, и это не скачок.
    """
    from app.services import telemetry_service

    t0 = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    # 11 км за 40 секунд — 990 км/ч
    reason = telemetry_service.gps_jump_reason(
        t0, 59.85, 30.42, t0 + timedelta(seconds=40), 59.95, 30.42
    )
    assert reason and "скачок GPS" in reason
    # час молчания — судить нельзя
    assert telemetry_service.gps_jump_reason(
        t0, 59.85, 30.42, t0 + timedelta(hours=1), 59.95, 30.42
    ) is None
    # обычная езда — не скачок
    assert telemetry_service.gps_jump_reason(
        t0, 59.85, 30.42, t0 + timedelta(seconds=40), 59.8512, 30.4212
    ) is None

    src = open("app/telemetry/egts_receiver.py", encoding="utf-8").read()
    assert src.count("telemetry_service.gps_jump_reason(") == 2   # оба протокола
    assert src.count("jump or \"нет достоверных координат (GPS)\"") == 2
