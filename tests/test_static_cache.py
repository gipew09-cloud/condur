"""Кэш браузера не должен прятать выкаченные правки.

Владелец 27.08.2026: «очисти кэш и всегда очищай кэш». Руками этого не
добиться — чистить пришлось бы в браузере каждого, кто открыл кабинет.
Поэтому чистка сделана автоматической: у каждой ссылки на /static своя метка
версии, и на новом деплое она меняется. Браузер видит новый адрес и идёт за
свежим файлом сам.
"""
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

pytest.importorskip("aiogram")

TEMPLATES = sorted(Path("app/web/templates").glob("*.html"))


def test_every_static_link_carries_a_version():
    """Ссылка без метки версии = файл, который однажды застрянет в кэше."""
    bad = []
    for path in TEMPLATES:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in re.finditer(r"/static/[A-Za-z0-9._-]+\.(?:png|css|js)", line):
                # метка стоит сразу за именем файла: ?v={{ asset_v }}
                tail = line[match.end():match.end() + 30]
                if "asset_v" not in tail:
                    bad.append(f"{path.name}:{line_no} {match.group(0)}{tail[:12]}")
    assert not bad, "ссылки на статику без метки версии:\n" + "\n".join(bad)


def test_version_is_not_written_by_hand():
    """Номер версии руками не проставляем — о нём забывают.

    Так и было: `cabinet.css?v=4` не менялся, стили менялись, и правка
    «не выкатывалась», потому что браузер отдавал файл из кэша.
    """
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\?v=\d", text), f"{path.name}: версия проставлена вручную"


def test_version_changes_between_deploys():
    """Новый деплой — новая метка. Иначе смысла в ней нет."""
    from app.web.router import _asset_version

    old = os.environ.get("RAILWAY_GIT_COMMIT_SHA")
    try:
        os.environ["RAILWAY_GIT_COMMIT_SHA"] = "aaaaaaaaaaaa1111"
        first = _asset_version()
        os.environ["RAILWAY_GIT_COMMIT_SHA"] = "bbbbbbbbbbbb2222"
        second = _asset_version()
        assert first != second
        assert len(first) <= 8, "метка должна быть короткой, это часть адреса"
    finally:
        os.environ.pop("RAILWAY_GIT_COMMIT_SHA", None)
        if old is not None:
            os.environ["RAILWAY_GIT_COMMIT_SHA"] = old


def test_version_works_without_railway():
    """Локально переменных деплоя нет — метка берётся из файлов /static."""
    from app.web.router import _asset_version

    saved = {n: os.environ.pop(n, None) for n in ("RAILWAY_GIT_COMMIT_SHA", "RAILWAY_DEPLOYMENT_ID")}
    try:
        value = _asset_version()
        assert value and value != "dev", "метка должна считаться по времени правки файлов"
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


def test_html_pages_are_never_served_from_browser_cache():
    """⚠️ Страницы кэшировать нельзя.

    Файлы в /static версионируются меткой, а сам HTML браузер мог отдать из
    своего кэша — и владелец видел вчерашнюю разметку, решая, что правка «не
    выкатилась». Владелец 31.08.2026: «кэш ты не очищаешь».
    """
    src = open("app/web/router.py", encoding="utf-8").read()
    assert 'response.headers.setdefault("Cache-Control", "no-store, must-revalidate")' in src
    assert 'startswith("text/html")' in src
