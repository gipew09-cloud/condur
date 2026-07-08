"""Сверка план↔факт: сопоставление назначения рейса с РЦ из справочника."""
from types import SimpleNamespace as NS

from app.services.rc_service import match_destination_to_center


def _rc(id, name, address="", aliases=None):
    return NS(id=id, name=name, address=address, aliases=aliases)


CENTERS = [
    _rc(1, "РЦ Дикси Шушары", "Московское шоссе, д.70", "Дикси"),
    _rc(2, "РЦ 7 шагов", "1-й Бадаевский проезд, д.7", "7 шагов; семь шагов"),
    _rc(3, "РЦ Адамант", "Московское шоссе, д.24", "Агроторг"),
]


def test_exact_name_match():
    rc = match_destination_to_center("РЦ Дикси Шушары", CENTERS)
    assert rc is not None and rc.id == 1


def test_alias_match():
    rc = match_destination_to_center("Агроторг", CENTERS)
    assert rc is not None and rc.id == 3


def test_substring_match():
    rc = match_destination_to_center("везу на 7 шагов сегодня", CENTERS)
    assert rc is not None and rc.id == 2


def test_no_match_returns_none():
    assert match_destination_to_center("склад в Мурманске", CENTERS) is None
    assert match_destination_to_center("", CENTERS) is None
    assert match_destination_to_center(None, CENTERS) is None


def test_different_rc_is_mismatch():
    # назначение — Дикси, а фактически приехал бы на 7 шагов → разные id
    planned = match_destination_to_center("Дикси", CENTERS)
    assert planned is not None and planned.id == 1
    assert planned.id != 2  # факт «7 шагов» → это mismatch
