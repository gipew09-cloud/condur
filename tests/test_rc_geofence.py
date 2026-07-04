"""Геозоны РЦ: расстояние (гаверсинус) и разбор ответа геокодера."""
from decimal import Decimal

from app.services.geocode_service import parse_nominatim_response
from app.services.rc_service import haversine_m


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
    """Порог выхода должен быть больше порога входа (гистерезис от дрожания GPS).
    scheduler_jobs импортирует aiogram (нет в локальном окружении) — читаем констант
    ы из исходника через ast, как в test_migrations."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/services/scheduler_jobs.py").read_text(encoding="utf-8"))
    consts = {
        t.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and t.id.startswith("RC_GEOFENCE")
    }
    assert consts["RC_GEOFENCE_RADIUS_M"] < consts["RC_GEOFENCE_EXIT_RADIUS_M"]


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
