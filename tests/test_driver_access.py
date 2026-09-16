"""Доступ водителя в приложение: выдача, вход, телефоны, отзыв.

Решения владельца 16.09.2026: паролей нет, доступ выдаёт владелец, у водителя
может быть 2–3 телефона, одного телефона на двоих не бывает. Остальное —
сошлись семь нейросетей (`DRIVER_ACCESS_AI_ANSWERS.md`).
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

pytest.importorskip("aiosqlite")

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from app.models import Base, Driver, DriverAccessGrant, Owner  # noqa: E402
from app.services import driver_access_service as access  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


_ENGINES = []


def _run(scenario):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        while _ENGINES:
            loop.run_until_complete(_ENGINES.pop().dispose())
        loop.close()


async def _db():
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    owner = Owner(telegram_id=1, full_name="Владелец", timezone="Europe/Moscow")
    other = Owner(telegram_id=2, full_name="Чужой парк", timezone="Europe/Moscow")
    session.add_all([owner, other])
    await session.flush()
    driver = Driver(owner_id=owner.id, full_name="Саломов Холбек", is_active=True)
    session.add(driver)
    await session.flush()
    return session, owner, other, driver


def _redeem(session, **kw):
    params = dict(
        token=None, code=None, device_id="phone-1", device_label="iPhone 15",
        platform="ios", app_version="1.0", ip="1.2.3.4",
    )
    params.update(kw)
    return access.redeem(session, **params)


def test_код_без_похожих_знаков_и_восемь_символов():
    """Код диктуют голосом и переписывают с экрана: 0/O и 1/I путаются."""
    async def scenario():
        session, owner, _, driver = await _db()
        issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        assert len(issued.code) == 8
        assert not set(issued.code) & set("0O1IL")
        assert access.format_code(issued.code) == f"{issued.code[:4]}-{issued.code[4:]}"
        await session.close()
    _run(scenario)


def test_в_базе_только_отпечатки():
    """Утечка базы не должна давать вход: ни ссылки, ни кода в ней нет."""
    async def scenario():
        session, owner, _, driver = await _db()
        issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        stored = await session.get(DriverAccessGrant, issued.grant.id)
        assert issued.token not in (stored.token_hash, stored.code_hash)
        assert issued.code not in (stored.token_hash, stored.code_hash)
        await session.close()
    _run(scenario)


def test_вход_по_коду_с_дефисом_и_строчными():
    async def scenario():
        session, owner, _, driver = await _db()
        issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        typed = access.format_code(issued.code).lower() + " "
        done = await _redeem(session, code=typed)
        assert done.driver.id == driver.id
        assert done.driver_session.device_id == "phone-1"
        # Сессия находится по токену из cookie, а в базе лежит только отпечаток.
        assert done.driver_session.token_hash != done.token
        found = await access.session_by_token(session, done.token)
        assert found is not None and found.id == done.driver_session.id
        await session.close()
    _run(scenario)


def test_вход_по_ссылке():
    async def scenario():
        session, owner, _, driver = await _db()
        issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        done = await _redeem(session, token=issued.token)
        assert done.driver.id == driver.id
        await session.close()
    _run(scenario)


def test_выдача_гасится_один_раз_и_ссылкой_и_кодом():
    """Ссылка и код — два способа погасить ОДНУ выдачу."""
    async def scenario():
        session, owner, _, driver = await _db()
        issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        await _redeem(session, token=issued.token)
        with pytest.raises(access.RedeemError, match="уже использован"):
            await _redeem(session, code=issued.code, device_id="phone-2")
        with pytest.raises(access.RedeemError, match="уже использован"):
            await _redeem(session, token=issued.token, device_id="phone-3")
        await session.close()
    _run(scenario)


def test_через_полчаса_выдача_не_работает():
    async def scenario():
        session, owner, _, driver = await _db()
        issued_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        issued = await access.issue_grant(
            session, driver=driver, issued_by_telegram_id=1, now=issued_at,
        )
        with pytest.raises(access.RedeemError, match="истёк"):
            await _redeem(session, code=issued.code)
        await session.close()
    _run(scenario)


def test_новая_выдача_сжигает_прежнюю():
    """Владелец нажал «Выдать» дважды — первая ссылка из переписки не должна
    остаться рабочей."""
    async def scenario():
        session, owner, _, driver = await _db()
        first = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        second = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        with pytest.raises(access.RedeemError, match="не подходят"):
            await _redeem(session, code=first.code)
        done = await _redeem(session, code=second.code)
        assert done.driver.id == driver.id
        await session.close()
    _run(scenario)


def test_неверный_код_не_подсказывает_ничего():
    async def scenario():
        session, owner, _, driver = await _db()
        await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        for wrong in ("AAAA-AAAA", "123", "", "ABCDEFGHJK"):
            with pytest.raises(access.RedeemError, match="не подходят"):
                await _redeem(session, code=wrong)
        await session.close()
    _run(scenario)


def test_до_трёх_телефонов_а_четвёртый_просит_владельца():
    """Владелец 16.09.2026: «у водителя может быть 2–3 телефона»."""
    async def scenario():
        session, owner, _, driver = await _db()
        for n in range(1, 4):
            issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
            await _redeem(session, code=issued.code, device_id=f"phone-{n}")
        assert len(await access.active_devices(session, driver.id)) == 3

        issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        with pytest.raises(access.RedeemError, match="отключить один"):
            await _redeem(session, code=issued.code, device_id="phone-4")
        await session.close()
    _run(scenario)


def test_повторный_вход_с_того_же_телефона_не_плодит_устройства():
    """Переустановили приложение на том же телефоне — это всё ещё один телефон."""
    async def scenario():
        session, owner, _, driver = await _db()
        first = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        old = await _redeem(session, code=first.code, device_id="phone-1")
        again = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        new = await _redeem(session, code=again.code, device_id="phone-1")
        devices = await access.active_devices(session, driver.id)
        assert [d.id for d in devices] == [new.driver_session.id]
        assert await access.session_by_token(session, old.token) is None
        await session.close()
    _run(scenario)


def test_отключить_все_телефоны_и_выдачу():
    """Потерял телефон или уволился: владелец отключает всё разом."""
    async def scenario():
        session, owner, _, driver = await _db()
        issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        done = await _redeem(session, code=issued.code)
        waiting = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)

        assert await access.revoke_all(session, driver=driver) == 1
        assert await access.session_by_token(session, done.token) is None
        with pytest.raises(access.RedeemError):
            await _redeem(session, code=waiting.code, device_id="phone-2")
        await session.close()
    _run(scenario)


def test_чужой_телефон_отключить_нельзя():
    async def scenario():
        session, owner, other, driver = await _db()
        issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        done = await _redeem(session, code=issued.code)
        assert await access.revoke_device(
            session, owner_id=other.id, session_id=done.driver_session.id,
        ) is None
        assert await access.session_by_token(session, done.token) is not None
        assert await access.revoke_device(
            session, owner_id=owner.id, session_id=done.driver_session.id,
        ) is not None
        assert await access.session_by_token(session, done.token) is None
        await session.close()
    _run(scenario)


def test_уволенному_водителю_вход_закрыт():
    async def scenario():
        session, owner, _, driver = await _db()
        issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        driver.is_active = False
        with pytest.raises(access.RedeemError, match="отключён"):
            await _redeem(session, code=issued.code)
        await session.close()
    _run(scenario)


def test_без_номера_устройства_входа_нет():
    async def scenario():
        session, owner, _, driver = await _db()
        issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=1)
        with pytest.raises(access.RedeemError, match="номер устройства"):
            await _redeem(session, code=issued.code, device_id="  ")
        await session.close()
    _run(scenario)
