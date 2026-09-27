"""Вход гаснет, если им не пользовались 90 дней (проверка безопасности 26.09.2026).

Иначе забытый старый телефон или чужой компьютер годами оставался открытой
дверью в кабинет владельца и в приложение водителя.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test-secret-please-set-a-long-one-in-prod")

import pytest  # noqa: E402

pytest.importorskip("aiosqlite")

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.models import Base, Driver, Owner, WebSession  # noqa: E402
from app.services import auth_service  # noqa: E402
from app.services import driver_access_service as access  # noqa: E402
from app.web import router as R  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint(type_, compiler, **kw):
    return "INTEGER"


def _run(body):
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await body(session)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def _request(cookie: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/",
        "headers": [(b"cookie", f"{auth_service.SESSION_COOKIE}={cookie}".encode())],
    })


def test_вход_владельца_гаснет_через_90_дней_тишины():
    async def body(session):
        owner = Owner(telegram_id=1, full_name="Владелец", timezone="Europe/Moscow")
        session.add(owner)
        await session.flush()
        now = datetime.now(timezone.utc)
        fresh, stale = auth_service.new_session_token(), auth_service.new_session_token()
        session.add_all([
            WebSession(owner_id=owner.id, telegram_id=1,
                       token_hash=auth_service.session_token_hash(fresh),
                       last_seen_at=now - timedelta(days=89)),
            WebSession(owner_id=owner.id, telegram_id=1,
                       token_hash=auth_service.session_token_hash(stale),
                       last_seen_at=now - timedelta(days=91)),
        ])
        await session.commit()
        assert await R._session_from_request(_request(fresh), session) is not None
        assert await R._session_from_request(_request(stale), session) is None
    _run(body)


def test_вход_водителя_гаснет_через_90_дней_тишины():
    async def body(session):
        owner = Owner(telegram_id=1, full_name="Владелец", timezone="Europe/Moscow")
        session.add(owner)
        await session.flush()
        driver = Driver(owner_id=owner.id, telegram_id=2, full_name="Водитель")
        session.add(driver)
        await session.flush()
        issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        done = await access.redeem(
            session, token=None, code=issued.code, device_id="phone-1",
            device_label="iPhone", platform="ios", app_version="1", ip=None,
        )
        await session.commit()
        now = datetime.now(timezone.utc)
        assert await access.session_by_token(session, done.token, now=now + timedelta(days=89))
        assert await access.session_by_token(session, done.token, now=now + timedelta(days=180)) is None
    _run(body)
