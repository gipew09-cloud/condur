"""Сводка за сутки — то, что в приложении открывает кнопка «Сводка».

Владелец 12.09.2026 показал такой экран у Ставтрэка: пробег, общее время,
время в движении и стоянки, средняя и максимальная скорость, моточасы,
холостой ход.

⚠️ Главное, что здесь проверяется, — честность. Где данных нет, должно быть
«нет данных», а не ноль; средняя скорость считается от времени В ДВИЖЕНИИ, а
не от суток; провал связи не идёт ни в моточасы, ни в стоянку.
"""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

pytest.importorskip("aiogram")

from app.services import telemetry_service as TS  # noqa: E402

START = datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)


def _drive(minutes: int, *, speed=60, ignition=True, lat0=59.9, step=0.01):
    """Минута за минутой: машина едет по прямой на север."""
    return [
        (START + timedelta(minutes=i), lat0 + step * i, 30.3, Decimal(speed), ignition)
        for i in range(minutes + 1)
    ]


def test_пустая_сводка_это_нет_данных_а_не_нули():
    empty = TS.day_summary([], window_end=START + timedelta(hours=1))
    # ⚠️ Ноль читается как факт «никуда не ездил». Это не так: точек не было.
    assert empty["distance_km"] is None
    assert empty["moving_seconds"] is None
    assert empty["engine_seconds"] is None
    assert empty["points"] == 0


def test_средняя_скорость_считается_от_времени_в_движении():
    # Полчаса ехали, потом три часа стояли с заглушенным двигателем.
    points = _drive(30)
    last_lat = points[-1][1]
    standing = [
        (START + timedelta(minutes=30 + i), last_lat, 30.3, Decimal(0), False)
        for i in range(1, 181)
    ]
    summary = TS.day_summary(points + standing, window_end=START + timedelta(hours=4))

    assert summary["distance_km"] > 0
    # ⚠️ Если поделить путь на ВСЁ время, получится бессмыслица — как «79 л на
    # 100 км» у конкурента, где топливо стоянки поделили на пробег.
    by_moving = summary["distance_km"] / (summary["moving_seconds"] / 3600)
    assert abs(summary["avg_speed_kmh"] - by_moving) <= 2
    assert summary["stop_seconds"] > summary["moving_seconds"]


def test_холостой_ход_это_мотор_работает_а_машина_стоит():
    lat = 59.9
    idling = [
        (START + timedelta(minutes=i), lat, 30.3, Decimal(0), True)
        for i in range(0, 41)
    ]
    summary = TS.day_summary(idling, window_end=START + timedelta(hours=1))

    assert summary["idle_seconds"] == pytest.approx(40 * 60, abs=120)
    # Мотор работал всё это время — моточасы не меньше холостого хода.
    assert summary["engine_seconds"] >= summary["idle_seconds"]
    assert summary["distance_km"] == 0


def test_без_датчика_зажигания_моточасов_нет_а_не_ноль():
    points = [
        (START + timedelta(minutes=i), 59.9 + 0.01 * i, 30.3, Decimal(60), None)
        for i in range(0, 21)
    ]
    summary = TS.day_summary(points, window_end=START + timedelta(hours=1))

    # ⚠️ Ноль моточасов означал бы «мотор не работал». Датчика просто нет.
    assert summary["engine_seconds"] is None
    assert summary["idle_seconds"] is None
    assert summary["distance_km"] > 0


def test_провал_связи_не_идёт_в_моточасы():
    # Пять минут езды, потом дыра в три часа, потом снова точка.
    points = _drive(5)
    resumed = [(START + timedelta(hours=3), 60.2, 30.3, Decimal(0), True)]
    summary = TS.day_summary(points + resumed, window_end=START + timedelta(hours=4))

    # ⚠️ В дыру мы не знаем, работал ли двигатель. Три часа моточасов из
    # воздуха — это выдумка.
    assert summary["engine_seconds"] < 30 * 60


def test_максимальная_скорость_берётся_из_самой_быстрой_точки():
    points = _drive(10, speed=40)
    points.append((START + timedelta(minutes=11), 60.1, 30.3, Decimal(93), True))
    summary = TS.day_summary(points, window_end=START + timedelta(hours=1))
    assert summary["max_speed_kmh"] == 93
