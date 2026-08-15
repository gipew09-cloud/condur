"""Регресс на HTML-инъекцию в боты (аудит безопасности 2026-08)."""
from app.services.textsanitize import clean_user_text


def test_strips_angle_brackets():
    # ссылка-приманка на казино не должна пережить очистку
    dirty = '<a href="http://casino">Агропарк</a>'
    clean = clean_user_text(dirty)
    assert "<" not in clean and ">" not in clean
    assert "Агропарк" in clean


def test_keeps_normal_text():
    assert clean_user_text("  Агропарк 5.18  ") == "Агропарк 5.18"
    assert clean_user_text("РЦ Лента → Магнит") == "РЦ Лента → Магнит"


def test_handles_none_and_empty():
    assert clean_user_text(None) == ""
    assert clean_user_text("") == ""
    assert clean_user_text("   ") == ""
