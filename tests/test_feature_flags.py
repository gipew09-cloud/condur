"""
Короткие тесты гейтинга фича-флагов бота водителя (Блоки A/B).

Запуск: `pip install pytest aiogram` затем `pytest` в корне проекта.
ENV-переменные ботов/БД здесь фиктивные — нужны только чтобы импортировать
настройки; до сети/БД тесты не доходят (проверяют сборку клавиатур).
"""
import os

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

from app.config import settings  # noqa: E402

pytest.importorskip("aiogram")

from app.bots import keyboards as kb  # noqa: E402


def _texts(markup) -> set[str]:
    return {btn.text for row in markup.keyboard for btn in row}


def test_no_shift_hides_cash_downtime_status_by_default():
    settings.feature_cash_handover = False
    settings.feature_downtime = False
    texts = _texts(kb.driver_no_shift_kb())
    assert kb.BTN_START_SHIFT in texts
    assert kb.BTN_ADD_SHIFT in texts and kb.BTN_ADD_TRIP in texts
    assert kb.BTN_HANDED_CASH not in texts
    assert kb.BTN_DOWNTIME not in texts
    assert kb.BTN_STATUS not in texts


def test_no_shift_shows_extras_when_flags_on():
    settings.feature_cash_handover = True
    settings.feature_downtime = True
    try:
        texts = _texts(kb.driver_no_shift_kb())
        assert kb.BTN_HANDED_CASH in texts
        assert kb.BTN_DOWNTIME in texts
    finally:
        settings.feature_cash_handover = False
        settings.feature_downtime = False


def test_in_transit_keyboard_follows_status_steps_flag():
    settings.feature_trip_status_steps = False
    off = _texts(kb.driver_trip_in_transit_kb())
    assert kb.BTN_END_TRIP in off and kb.BTN_TRIP_UNLOADING not in off

    settings.feature_trip_status_steps = True
    try:
        on = _texts(kb.driver_trip_in_transit_kb())
        assert kb.BTN_TRIP_UNLOADING in on
    finally:
        settings.feature_trip_status_steps = False


def test_status_button_removed_from_all_driver_keyboards():
    for factory in (
        kb.driver_no_shift_kb,
        kb.driver_shift_no_trip_kb,
        kb.driver_trip_created_kb,
        kb.driver_trip_in_transit_kb,
        kb.driver_trip_unloading_kb,
    ):
        assert kb.BTN_STATUS not in _texts(factory())
