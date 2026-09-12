"""Два человека в приложении: владелец и админ его кабинета.

Владелец 10.09.2026: «хочу сделать сборку, скачать на Android и потом войти под
аккаунтом владельца — и будет два владельца… то есть чтобы было два админа
приложения. Протестируй и такой вариант».

Проверяем именно то, что может сломаться: вход админа через ту же форму, что и
у владельца; оба устройства живут одновременно; оба видят один и тот же парк;
и отзыв админа закрывает доступ сразу, а не когда-нибудь.
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

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import BigInteger, delete  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.models import (  # noqa: E402
    Admin, Base, Driver, Event, Owner, Shift, Vehicle,
)
from app.services import auth_service  # noqa: E402
from app.web.router import api_events, current_owner, login_submit  # noqa: E402

NOW = datetime.now(timezone.utc)

_ENGINES = []


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


def _run(scenario):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        while _ENGINES:
            loop.run_until_complete(_ENGINES.pop().dispose())
        loop.close()


def _request(user_agent: str = "Condur App (android)", cookie: str | None = None):
    """Минимальный HTTP-запрос: вход смотрит только на заголовки и адрес."""
    headers = [(b"user-agent", user_agent.encode())]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/login",
        "headers": headers,
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("10.0.0.1", 5555),
    })


def _token_from(response) -> str:
    """Достать токен сессии из ответа — ровно так же его достаёт приложение."""
    raw = response.headers["set-cookie"]
    return raw.split(";")[0].split("=", 1)[1]


async def _db():
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    owner = Owner(telegram_id=111, full_name="Владелец", timezone="Europe/Moscow")
    session.add(owner)
    await session.flush()
    admin = Admin(owner_id=owner.id, telegram_id=222, notifications_enabled=True)
    vehicle = Vehicle(owner_id=owner.id, license_plate="Т557ОС178", is_active=True)
    driver = Driver(owner_id=owner.id, full_name="Саломов", telegram_id=333)
    session.add_all([admin, vehicle, driver])
    await session.flush()
    # ⚠️ Смена настоящая: событие «Смена начата» без смены лента считает
    # сиротой от удалённой смены и не показывает.
    shift = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                  started_at=NOW - timedelta(hours=1), status="started")
    session.add(shift)
    await session.flush()
    session.add(Event(
        owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
        event_type="shift_started", created_at=NOW - timedelta(hours=1),
    ))
    await session.commit()
    return session, owner, admin


async def _sign_in(session, telegram_id: int, user_agent: str):
    code = auth_service.issue_code(telegram_id)
    response = await login_submit(
        _request(user_agent), str(telegram_id), code, session
    )
    assert response.status_code == 303
    return _token_from(response)


def test_админ_входит_в_приложение_и_видит_парк_владельца():
    async def scenario():
        session, owner, admin = await _db()
        token = await _sign_in(session, admin.telegram_id, "Condur App (android)")

        seen = await current_owner(_request(cookie=f"session={token}"), session)
        # ⚠️ Админ получает не свой кабинет, а кабинет ВЛАДЕЛЬЦА: парк общий.
        assert seen.id == owner.id
        rows = (await api_events(seen, session))["events"]
        assert [e["type"] for e in rows] == ["shift_started"]
        await session.close()
    _run(scenario)


def test_два_устройства_работают_одновременно():
    async def scenario():
        session, owner, admin = await _db()
        phone = await _sign_in(session, owner.telegram_id, "Condur App (ios)")
        tablet = await _sign_in(session, admin.telegram_id, "Condur App (android)")

        # Вход со второго устройства не гасит первое — это разные сессии.
        first = await current_owner(_request(cookie=f"session={phone}"), session)
        second = await current_owner(_request(cookie=f"session={tablet}"), session)
        assert first.id == second.id == owner.id
        assert phone != tablet
        await session.close()
    _run(scenario)


def test_устройства_подписаны_по_разному():
    async def scenario():
        session, owner, admin = await _db()
        await _sign_in(session, owner.telegram_id, "Condur App (ios)")
        await _sign_in(session, admin.telegram_id, "Condur App (android)")

        from app.models import WebSession
        from sqlalchemy import select

        labels = [
            row[0] for row in (
                await session.execute(select(WebSession.device_label))
            ).all()
        ]
        # В «Реквизиты → Устройства» владелец должен различать телефоны.
        assert len(set(labels)) == 2
        assert any("Condur" in (label or "") for label in labels)
        await session.close()
    _run(scenario)


def test_отзыв_админа_закрывает_приложение_сразу():
    async def scenario():
        session, owner, admin = await _db()
        token = await _sign_in(session, admin.telegram_id, "Condur App (android)")
        assert (await current_owner(_request(cookie=f"session={token}"), session)).id == owner.id

        await session.execute(delete(Admin).where(Admin.telegram_id == admin.telegram_id))
        await session.commit()

        # ⚠️ Не «когда истечёт сессия», а в тот же миг: сессия жива, но доступа нет.
        with pytest.raises(HTTPException):
            await current_owner(_request(cookie=f"session={token}"), session)
        await session.close()
    _run(scenario)


def test_посторонний_в_приложение_не_войдёт():
    async def scenario():
        session, owner, admin = await _db()
        stranger = 999
        code = auth_service.issue_code(stranger)
        response = await login_submit(
            _request(), str(stranger), code, session
        )
        # Код бот выдаёт кому угодно, но доступ — только владельцу и его админам.
        assert response.status_code == 400
        assert "set-cookie" not in response.headers
        await session.close()
    _run(scenario)
