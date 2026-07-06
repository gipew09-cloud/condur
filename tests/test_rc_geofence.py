"""Геозоны РЦ: расстояние (гаверсинус), разбор ответа геокодера,
стоянка/мотор, рендер страницы статистики."""
import os
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace as NS

from jinja2 import Environment, FileSystemLoader

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

from app.services.geocode_service import parse_nominatim_response
from app.services.rc_service import haversine_m
from app.services.timeutil import fmt_dt


def test_render_stats_page():
    """Страница /stats рендерится с журналом простоев и сводками."""
    env = Environment(loader=FileSystemLoader("app/web/templates"))
    env.filters["localdt"] = lambda dt, tz=None, fmt="%d.%m.%Y %H:%M": fmt_dt(dt, tz, fmt)
    env.filters["pillclass"] = lambda s: "pill--neutral"
    env.filters["tstatus"] = lambda s: s or "—"
    env.filters["vtype"] = lambda s: s or "—"
    env.filters["statusru"] = lambda s: s or "—"
    owner = NS(company_name="БРО ЛОГИСТИК", timezone="Europe/Moscow")
    when = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)
    html = env.get_template("stats.html").render(
        owner=owner, active_page="stats",
        period_from="2026-07-01", period_to="2026-07-05",
        kpi={
            "visits": 3, "total_wait": "5 ч 10 мин",
            "avg_wait": "1 ч 43 мин", "billable_label": "8 000 ₽",
        },
        journal=[{
            "arrived_at": when, "departed_at": when,
            "plate": "Т772НХ178", "driver": "Иван", "rc_name": "Дикси Шушары",
            "rc_id": 1, "waited_minutes": 260, "waited_label": "4 ч 20 мин",
            "engine_off_label": "3 ч 50 мин", "billable_label": "8 000 ₽",
        }],
        rc_summary=[{"name": "Дикси Шушары", "visits": 3,
                     "total_label": "5 ч 10 мин", "avg_label": "1 ч 43 мин",
                     "total": 310, "billable_label": "8 000 ₽", "billable": 8000}],
        driver_summary=[{"name": "Иван", "trips": 5, "km": 320,
                         "idle_label": "4 ч 20 мин", "idle_minutes": 260,
                         "billable_label": "8 000 ₽", "billable": 8000}],
        week_summary=[{"week": "нед. 29.06", "trips": 7, "revenue": Decimal("106000")}],
        live_issues=[{
            "sev": "danger", "icon": "📡", "title": "GPS давно не обновлялся",
            "pill": "5 ч", "sub": "Т772НХ178 · последний сигнал 03.07 21:10",
            "href": "/map",
        }],
        live_issue_counts={"total": 1, "danger": 1, "warn": 0, "info": 0},
    )
    assert "Журнал простоев" in html
    assert "Т772НХ178" in html and "Дикси Шушары" in html
    assert "4 ч 20 мин" in html and "3 ч 50 мин" in html
    assert "Потенциально к выставлению" in html and "8 000 ₽" in html
    assert "106 000" in html
    assert "Операционный контроль сейчас" in html
    assert "GPS давно не обновлялся" in html and "1 срочно" in html


def test_haversine_zero_distance():
    assert haversine_m(59.93, 30.33, 59.93, 30.33) == 0


def test_haversine_known_distance_spb_to_moscow():
    # СПб (Дворцовая) → Москва (Красная площадь) ≈ 634 км по прямой
    d = haversine_m(59.9386, 30.3141, 55.7539, 37.6208)
    assert 620_000 < d < 650_000


def test_haversine_short_distance_meters():
    # ~111 м на градус широты 0.001 (у любой долготы)
    d = haversine_m(59.900000, 30.300000, 59.901000, 30.300000)
    assert 100 < d < 125


def test_haversine_accepts_decimal():
    d = haversine_m(Decimal("59.9"), Decimal("30.3"), Decimal("59.9"), Decimal("30.31"))
    assert 500 < d < 600  # ~560 м на этой широте


def test_geofence_radius_hysteresis():
    """Выходной радиус больше входного (гистерезис) и стоянка перед «приехал»
    обязательна. scheduler_jobs импортирует aiogram (нет в локальном окружении) —
    читаем литеральные константы из исходника через ast, как в test_migrations."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/services/scheduler_jobs.py").read_text(encoding="utf-8"))
    consts = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id.startswith("RC_"):
                try:
                    consts[t.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass  # вычисляемые (например, EXIT_RADIUS = int(...)) пропускаем
    assert consts["RC_GEOFENCE_EXIT_FACTOR"] > 1
    assert consts["RC_MIN_PARKED_MINUTES"] >= 1
    assert consts["RC_EVENTS_LOOKBACK_DAYS"] >= 30


def test_minutes_label_and_safe_int_for_dirty_event_payloads():
    """Статистика не должна падать, если в старом Event.payload лежит строка
    или мусор вместо числа."""
    from app.services.telemetry_service import int_or_none, minutes_label

    assert int_or_none("260") == 260
    assert int_or_none("bad") is None
    assert minutes_label("260") == "4 ч 20 мин"
    assert minutes_label(None) == "—"
    assert minutes_label("bad") == "—"
    assert minutes_label("-5") == "0 мин"


def test_rc_billable_downtime_threshold():
    """12 часов на РЦ дают потенциальные 8000 ₽, но раньше порога — нет."""
    from app.services.telemetry_service import (
        RC_BILLABLE_DOWNTIME_RUB,
        RC_BILLABLE_WAIT_MINUTES,
        rc_billable_downtime_rub,
        rub_label,
    )

    assert RC_BILLABLE_WAIT_MINUTES == 12 * 60
    assert RC_BILLABLE_DOWNTIME_RUB == 8000
    assert rc_billable_downtime_rub(RC_BILLABLE_WAIT_MINUTES - 1) == 0
    assert rc_billable_downtime_rub(RC_BILLABLE_WAIT_MINUTES) == 8000
    assert rc_billable_downtime_rub("bad") == 0
    assert rub_label(8000) == "8 000 ₽"


def test_scheduler_records_single_rc_downtime_alert_source():
    """В геозонах должен быть отдельный одноразовый alert, а не автозапись
    денег в финансы. scheduler_jobs импортирует aiogram, поэтому проверяем
    источник без импорта модуля."""
    from pathlib import Path

    source = Path("app/services/scheduler_jobs.py").read_text(encoding="utf-8")
    assert '"rc_downtime_alert"' in source
    assert "pending_owner_decision" in source
    assert "Пока деньги не добавляю автоматически" in source


def test_parse_nominatim_ok():
    items = [{"lat": "59.8712", "lon": "30.4432", "display_name": "Шушары"}]
    assert parse_nominatim_response(items) == (Decimal("59.8712"), Decimal("30.4432"))


def test_parse_nominatim_empty_and_garbage():
    assert parse_nominatim_response([]) is None
    assert parse_nominatim_response(None) is None
    assert parse_nominatim_response("error") is None
    assert parse_nominatim_response([{"nolat": 1}]) is None
    assert parse_nominatim_response([{"lat": "abc", "lon": "30"}]) is None


def test_parse_nominatim_null_island_rejected():
    assert parse_nominatim_response([{"lat": "0.0", "lon": "0.0"}]) is None


def test_sum_engine_off_seconds():
    """Время с заглушенным двигателем: интервал приписывается состоянию первой
    точки; длинные дыры (трекер молчал) не считаются."""
    from datetime import datetime, timedelta, timezone

    from app.services.telemetry_service import sum_engine_off_seconds

    t0 = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)
    pt = lambda minutes, ign: (t0 + timedelta(minutes=minutes), ign)  # noqa: E731

    # 0-10 мин заглушен, 10-20 работает, 20-30 заглушен → 20 мин = 1200 с
    points = [pt(0, False), pt(10, True), pt(20, False), pt(30, True)]
    assert sum_engine_off_seconds(points) == 1200

    # дыра 30 минут (больше cap 600 с) не приписывается никому
    points = [pt(0, False), pt(30, False), pt(35, False)]
    assert sum_engine_off_seconds(points) == 300  # только 30→35

    # ignition None не считается выключенным
    points = [pt(0, None), pt(5, False), pt(10, False)]
    assert sum_engine_off_seconds(points) == 300  # только 5→10

    assert sum_engine_off_seconds([]) == 0
    assert sum_engine_off_seconds([pt(0, False)]) == 0


def test_parked_long_enough_filters_drive_by_and_short_stops():
    """Ключ анти-ложных срабатываний: «приехал» только после реальной стоянки.
    Едет мимо / стоит на светофоре пару минут — НЕ приехал."""
    from datetime import datetime, timedelta, timezone

    from app.services.telemetry_service import parked_long_enough

    now = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    ago = lambda minutes: now - timedelta(minutes=minutes)  # noqa: E731

    # едет мимо РЦ — не считается, сколько бы ни «стоял» статус
    assert parked_long_enough("moving", ago(30), now, 4) is False
    # встал на светофоре 2 минуты назад — рано
    assert parked_long_enough("stopped", ago(2), now, 4) is False
    # стоит 10 минут с заглушенным мотором — приехал
    assert parked_long_enough("stopped", ago(10), now, 4) is True
    # стоит 10 минут с работающим мотором (выгрузка с рефрижератором) — приехал
    assert parked_long_enough("idle_engine", ago(10), now, 4) is True
    # нет данных о начале стоянки — не рискуем
    assert parked_long_enough("stopped", None, now, 4) is False
    # naive datetime из БД не роняет сравнение
    assert parked_long_enough("stopped", now.replace(tzinfo=None) - timedelta(minutes=10), now, 4) is True
