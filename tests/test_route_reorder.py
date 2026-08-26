"""Перестановка маршрутов ▲▼ — проверка на живой БД, а не по тексту кода.

Владелец 27.08.2026: «в первом складе всё меняется хорошо, а если пролистать
на склад ниже и понажимать — телепортируется вниз в конец страницы».

Причина оказалась не в анимации и не в вёрстке: экран группировал склады по
ОБРЕЗАННОМУ названию, а перестановка искала соседей по названию КАК ЕСТЬ.
Строка с лишним пробелом («Соф.60 » вместо «Соф.60») показывалась внутри
группы, но для перестановки лежала в своей собственной — стрелки у неё
нажимались вхолостую, а сама она навсегда оставалась в конце списка.
"""
import asyncio
import os

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

from app.models import Base, Owner, RouteTemplate  # noqa: E402
from app.web.router import (  # noqa: E402
    _origin_key,
    _route_templates_view,
    routes_template_move,
)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


class _PlainRequest:
    """Не-HTMX запрос: эндпоинт вернёт редирект и не полезет рисовать шаблон."""
    headers: dict = {}


def _run(scenario):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        loop.close()


async def _db_with(rows):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    session = sessionmaker()
    owner = Owner(telegram_id=1, full_name="Владелец")
    session.add(owner)
    await session.flush()
    for origin, destination in rows:
        session.add(RouteTemplate(
            owner_id=owner.id, name=destination, origin=origin,
            destination=destination, sort_order=0, is_active=True,
        ))
    await session.commit()
    return session, owner


async def _order(session, owner_id, origin_key):
    by_origin, _ = await _route_templates_view(session, owner_id, [])
    return [t.destination for t in by_origin[origin_key]]


async def _press(session, owner, destination, direction):
    by_origin, _ = await _route_templates_view(session, owner.id, [])
    tmpl = next(
        t for items in by_origin.values() for t in items if t.destination == destination
    )
    await routes_template_move(tmpl.id, _PlainRequest(), owner, session, direction)


def test_origin_key_merges_stray_spaces():
    assert _origin_key("Соф.60 ") == _origin_key("Соф.60") == "Соф.60"
    assert _origin_key(None) == "—"
    assert _origin_key("") == "—"


def test_move_works_in_every_warehouse_not_only_the_first():
    """Второй склад должен переставляться так же, как первый."""
    async def scenario():
        session, owner = await _db_with([
            ("5.18 Склад", "Альфа"), ("5.18 Склад", "Бета"), ("5.18 Склад", "Гамма"),
            ("Соф.60", "Дельта"), ("Соф.60", "Епсилон"), ("Соф.60", "Зета"),
        ])
        assert await _order(session, owner.id, "Соф.60") == ["Дельта", "Епсилон", "Зета"]
        await _press(session, owner, "Дельта", "down")
        assert await _order(session, owner.id, "Соф.60") == ["Епсилон", "Дельта", "Зета"]
        # первый склад при этом не тронут
        assert await _order(session, owner.id, "5.18 Склад") == ["Альфа", "Бета", "Гамма"]
        await session.close()
    _run(scenario)


def test_row_with_stray_space_in_warehouse_moves_like_the_rest():
    """Строка с пробелом в названии склада больше не прилипает к концу списка.

    Это и есть баг, который увидел владелец. До правки «Гамма» лежала в своей
    собственной группе для перестановки, стрелки у неё не работали, и она
    оставалась внизу, куда бы её ни двигали.
    """
    async def scenario():
        session, owner = await _db_with([
            ("Соф.60", "Альфа"),
            ("Соф.60", "Бета"),
            ("Соф.60 ", "Гамма"),      # ← лишний пробел, но склад тот же
        ])
        # на экране это ОДНА группа из трёх строк
        by_origin, _ = await _route_templates_view(session, owner.id, [])
        assert list(by_origin) == ["Соф.60"]
        assert [t.destination for t in by_origin["Соф.60"]] == ["Альфа", "Бета", "Гамма"]

        await _press(session, owner, "Гамма", "up")
        assert await _order(session, owner.id, "Соф.60") == ["Альфа", "Гамма", "Бета"]
        await _press(session, owner, "Гамма", "up")
        assert await _order(session, owner.id, "Соф.60") == ["Гамма", "Альфа", "Бета"]

        # и данные вылечились: пробел убран, второй «склад» больше не заведётся
        origins = {t.origin for items in
                   (await _route_templates_view(session, owner.id, []))[0].values()
                   for t in items}
        assert origins == {"Соф.60"}
        await session.close()
    _run(scenario)


def test_move_never_leaks_into_a_neighbouring_warehouse():
    """Сосед берётся только внутри своего склада — чужие строки не двигаются."""
    async def scenario():
        session, owner = await _db_with([
            ("А", "а1"), ("А", "а2"), ("Б", "б1"), ("Б", "б2"),
        ])
        await _press(session, owner, "а2", "down")     # уже последняя в своём складе
        assert await _order(session, owner.id, "А") == ["а1", "а2"]
        assert await _order(session, owner.id, "Б") == ["б1", "б2"]
        await session.close()
    _run(scenario)
