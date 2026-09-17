"""Приложение водителя через настоящие адреса сервера.

Проверяется путь целиком, как его пройдёт телефон: владелец выдаёт доступ в
кабинете → водитель входит кодом → видит главный экран → начинает и
заканчивает смену → повтор не плодит дублей → владелец отключает телефон.
И отдельно — что вход водителя не открывает кабинет владельца, и наоборот.
"""
import asyncio
import json
import os
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

pytest.importorskip("aiogram")
pytest.importorskip("aiosqlite")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import BigInteger, select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.models import Base, Driver, Event, Owner, Shift, Vehicle, WebSession  # noqa: E402
from app.services import auth_service  # noqa: E402
from app.web import router as web  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"



@pytest.fixture(autouse=True)
def _photo_mode_off(monkeypatch):
    """Эти проверки — про саму смену, а не про фото одометра: фото-режим
    выключен. Фото проверяются в test_driver_photos.py."""
    from app.config import settings
    monkeypatch.setattr(settings, "feature_odometer_photo", False)

_ENGINES = []


def _run(scenario):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        while _ENGINES:
            loop.run_until_complete(_ENGINES.pop().dispose())
        loop.close()


class _App:
    """То, что роутер берёт из request.app: бот владельца для уведомлений."""
    def __init__(self):
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(message_id=1)
        self.state = SimpleNamespace(owner_bot=bot, driver_bot=AsyncMock())


def _request(app, *, method="POST", path="/", cookie=None, body=None):
    headers = [(b"user-agent", b"Condur App (ios)")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    raw = json.dumps(body).encode() if body is not None else b""
    if body is not None:
        headers.append((b"content-type", b"application/json"))

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request({
        "type": "http", "method": method, "path": path, "headers": headers,
        "query_string": b"", "scheme": "http", "server": ("test", 80),
        "client": ("10.0.0.1", 5555), "app": app,
    }, receive)


def _cookie(response, name):
    for key, value in response.raw_headers:
        if key == b"set-cookie" and value.decode().startswith(name + "="):
            return value.decode().split(";")[0]
    return None


def _json(response):
    return json.loads(response.body)


async def _db():
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    owner = Owner(telegram_id=111, full_name="Кибиткина", company_name="ИП Кибиткина",
                  timezone="Europe/Moscow", notifications_enabled=True)
    session.add(owner)
    await session.flush()
    vehicle = Vehicle(owner_id=owner.id, license_plate="Т557ОС178", is_active=True)
    driver = Driver(owner_id=owner.id, full_name="Саломов Холбек",
                    salary_type="per_km", salary_rate=10, is_active=True)
    session.add_all([vehicle, driver])
    # Вход владельца в кабинет — обычная сессия.
    owner_token = auth_service.new_session_token()
    session.add(WebSession(
        owner_id=owner.id, telegram_id=owner.telegram_id,
        token_hash=auth_service.session_token_hash(owner_token),
    ))
    await session.commit()
    return session, owner, vehicle, driver, f"session={owner_token}"


async def _issue_code(app, session, owner, driver, owner_cookie):
    req = _request(app, path=f"/drivers/{driver.id}/app/grant", cookie=owner_cookie)
    response = await web.driver_app_grant(req, driver.id, owner, session)
    page = response.body.decode()
    code = re.search(r'id="drv-code-\d+">([A-Z0-9-]+)<', page).group(1)
    link = re.search(r'value="(http[^"]+/d/[^"]+)"', page).group(1)
    return code, link, page


async def _redeem(app, session, **body):
    body.setdefault("device_id", "iphone-15-owner-test")
    body.setdefault("device_label", "iPhone 15")
    body.setdefault("platform", "ios")
    body.setdefault("app_version", "1.0.0")
    req = _request(app, path="/api/driver/redeem", body=body)
    return await web.api_driver_redeem(req, session)


async def _driver_ctx(app, session, cookie):
    return await web.current_driver(_request(app, method="GET", cookie=cookie), session)


def test_путь_водителя_целиком():
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()

        code, link, page = await _issue_code(app, session, owner, driver, owner_cookie)
        assert "Действует до" in page
        assert "/d/" in link

        response = await _redeem(app, session, code=code)
        assert response.status_code == 200
        assert _json(response)["driver"]["full_name"] == "Саломов Холбек"
        cookie = _cookie(response, "driver_session")
        assert cookie
        # Владельцу ушло «вошёл в приложение».
        texts = [c.args[1] for c in app.state.owner_bot.send_message.await_args_list]
        assert any("вошёл в приложение" in t and "iPhone 15" in t for t in texts)

        ctx = await _driver_ctx(app, session, cookie)
        me = await web.api_driver_me(ctx, session)
        assert me["shift"] is None
        assert me["company"] == "ИП Кибиткина"
        assert me["vehicles"] == [{"id": vehicle.id, "plate": "Т557ОС178", "busy": False, "color": "black"}]

        start_body = {
            "client_op_id": "a1b2c3d4-start", "type": "shift.start",
            "payload": {"vehicle_id": vehicle.id, "odometer": 1913},
        }
        req = _request(app, path="/api/driver/actions", cookie=cookie, body=start_body)
        started = _json(await web.api_driver_actions(req, ctx, session))
        assert started["ok"] is True and started["plate"] == "Т557ОС178"

        # Ответ потерялся — телефон повторил тот же номер.
        req = _request(app, path="/api/driver/actions", cookie=cookie, body=start_body)
        again = _json(await web.api_driver_actions(req, ctx, session))
        assert again["duplicate"] is True and again["shift_id"] == started["shift_id"]
        assert len((await session.execute(select(Shift))).scalars().all()) == 1

        me = await web.api_driver_me(ctx, session)
        assert me["shift"]["id"] == started["shift_id"]
        assert me["shift"]["odometer_start"] == 1913

        req = _request(app, path="/api/driver/actions", cookie=cookie, body={
            "client_op_id": "a1b2c3d4-finish", "type": "shift.finish",
            "payload": {"shift_id": started["shift_id"], "odometer": 1977},
        })
        finished = _json(await web.api_driver_actions(req, ctx, session))
        assert finished["ok"] is True and finished["distance_km"] == 64
        texts = [c.args[1] for c in app.state.owner_bot.send_message.await_args_list]
        assert any("завершил смену" in t and "Пробег: 64 км" in t for t in texts)
        await session.close()
    _run(scenario)


def test_кривой_запрос_от_телефона_это_400_а_не_падение():
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        code, _, _ = await _issue_code(app, session, owner, driver, owner_cookie)
        cookie = _cookie(await _redeem(app, session, code=code), "driver_session")
        ctx = await _driver_ctx(app, session, cookie)
        req = _request(app, path="/api/driver/actions", cookie=cookie,
                       body={"client_op_id": "x", "type": "shift.start"})
        response = await web.api_driver_actions(req, ctx, session)
        assert response.status_code == 400
        assert _json(response)["status"] == "bad_request"

        bad = _request(app, path="/api/driver/redeem", body=None)
        with pytest.raises(HTTPException) as err:
            await web.api_driver_redeem(bad, session)
        assert err.value.status_code == 400
        await session.close()
    _run(scenario)


def test_неверный_код_понятный_отказ():
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        response = await _redeem(app, session, code="AAAA-BBBB")
        assert response.status_code == 400
        assert "не подходят" in _json(response)["message"]
        assert _cookie(response, "driver_session") is None
        await session.close()
    _run(scenario)


def test_вход_водителя_не_открывает_кабинет_владельца():
    """⚠️ Главное про безопасность: разные таблицы и разные cookie."""
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        code, _, _ = await _issue_code(app, session, owner, driver, owner_cookie)
        driver_cookie = _cookie(await _redeem(app, session, code=code), "driver_session")
        token = driver_cookie.split("=", 1)[1]

        # Cookie водителя, положенная под именем cookie кабинета, — не вход.
        for cookie in (driver_cookie, f"session={token}"):
            with pytest.raises(HTTPException) as err:
                await web.current_owner(_request(app, method="GET", cookie=cookie), session)
            assert err.value.status_code == 303

        # И наоборот: сессия владельца не пускает в API водителя.
        owner_token = owner_cookie.split("=", 1)[1]
        for cookie in (owner_cookie, f"driver_session={owner_token}", None):
            with pytest.raises(HTTPException) as err:
                await _driver_ctx(app, session, cookie)
            assert err.value.status_code == 401
        await session.close()
    _run(scenario)


def test_отключённый_телефон_сразу_теряет_доступ():
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        code, _, _ = await _issue_code(app, session, owner, driver, owner_cookie)
        cookie = _cookie(await _redeem(app, session, code=code), "driver_session")
        ctx = await _driver_ctx(app, session, cookie)

        panel = await web.driver_app_panel(
            _request(app, method="GET", cookie=owner_cookie), driver.id, owner, session,
        )
        page = panel.body.decode()
        assert "iPhone 15" in page and "1 из 3" in page
        # Код второй раз не показывается — в базе его нет.
        assert 'id="drv-code-' not in page

        req = _request(app, cookie=owner_cookie)
        page = (await web.driver_app_revoke_device(
            req, driver.id, ctx.session.id, owner, session,
        )).body.decode()
        assert "Телефон отключён" in page
        with pytest.raises(HTTPException) as err:
            await _driver_ctx(app, session, cookie)
        assert err.value.status_code == 401
        await session.close()
    _run(scenario)


def test_отключить_все_не_закрывает_смену():
    """Открытую смену закрывает владелец, а не кнопка «отключить телефон»."""
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        code, _, _ = await _issue_code(app, session, owner, driver, owner_cookie)
        cookie = _cookie(await _redeem(app, session, code=code), "driver_session")
        ctx = await _driver_ctx(app, session, cookie)
        req = _request(app, cookie=cookie, body={
            "client_op_id": "keep-open-0001", "type": "shift.start",
            "payload": {"vehicle_id": vehicle.id},
        })
        await web.api_driver_actions(req, ctx, session)

        page = (await web.driver_app_revoke_all(
            _request(app, cookie=owner_cookie), driver.id, owner, session,
        )).body.decode()
        assert "Все телефоны отключены" in page
        shift = (await session.execute(select(Shift))).scalar_one()
        assert shift.status == "started"
        events = [e.event_type for e in (await session.execute(select(Event))).scalars()]
        assert "driver_devices_revoked" in events
        await session.close()
    _run(scenario)


def test_чужой_водитель_недоступен_в_кабинете():
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        stranger = Owner(telegram_id=999, full_name="Чужой", timezone="Europe/Moscow")
        session.add(stranger)
        await session.commit()
        for call in (
            lambda: web.driver_app_panel(_request(app), driver.id, stranger, session),
            lambda: web.driver_app_grant(_request(app), driver.id, stranger, session),
            lambda: web.driver_app_revoke_all(_request(app), driver.id, stranger, session),
            lambda: web.api_driver_app_access(driver.id, stranger, session),
        ):
            with pytest.raises(HTTPException) as err:
                await call()
            assert err.value.status_code == 404
        await session.close()
    _run(scenario)


def test_страница_ссылки_ничего_не_гасит():
    """Мессенджер открыл ссылку ради превью — выдача должна остаться рабочей."""
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        code, link, _ = await _issue_code(app, session, owner, driver, owner_cookie)
        token = link.rsplit("/d/", 1)[1]
        page = await web.driver_link_page(_request(app, method="GET"), token, session)
        assert page.status_code == 200
        body = page.body.decode()
        assert f"condur://d/{token}" in body
        assert "Открываю приложение" in body     # пробует открыть само
        assert "Саломов" not in body            # о водителе ни слова
        assert page.headers["referrer-policy"] == "no-referrer"
        with pytest.raises(HTTPException):
            await web.driver_link_page(_request(app, method="GET"), "../../etc", session)
        # Ссылка всё ещё гасится.
        response = await _redeem(app, session, token=token)
        assert response.status_code == 200
        # После входа страница честно говорит, что ссылка использована,
        # и кнопки входа больше нет.
        used = (await web.driver_link_page(_request(app, method="GET"), token, session)).body.decode()
        assert "уже вошли" in used and "condur://" not in used
        await session.close()
    _run(scenario)


def test_страница_старой_и_чужой_ссылки():
    """Владелец 17.09: «почему вчерашняя ссылка всё ещё работает?» — страница
    открывалась для любой ссылки. Теперь говорит, что с ней."""
    from datetime import timedelta

    from app.models import DriverAccessGrant

    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        _, link, _ = await _issue_code(app, session, owner, driver, owner_cookie)
        token = link.rsplit("/d/", 1)[1]
        grant = (await session.execute(select(DriverAccessGrant))).scalar_one()
        grant.expires_at = grant.created_at - timedelta(minutes=1)
        await session.commit()
        old = (await web.driver_link_page(_request(app, method="GET"), token, session)).body.decode()
        assert "Срок ссылки истёк" in old and "condur://" not in old

        unknown = "x" * 43
        page = (await web.driver_link_page(_request(app, method="GET"), unknown, session)).body.decode()
        assert "не действует" in page and "condur://" not in page

        # Новая выдача гасит старую ссылку — страница это видит.
        _, fresh, _ = await _issue_code(app, session, owner, driver, owner_cookie)
        grant.expires_at = grant.created_at + timedelta(minutes=30)
        await session.commit()
        revoked = (await web.driver_link_page(_request(app, method="GET"), token, session)).body.decode()
        assert "не действует" in revoked
        await session.close()
    _run(scenario)


def test_ссылка_на_android_открывает_приложение_через_intent():
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        _, link, _ = await _issue_code(app, session, owner, driver, owner_cookie)
        token = link.rsplit("/d/", 1)[1]
        req = _request(app, method="GET", path=f"/d/{token}")
        req.scope["headers"] = [(b"user-agent", b"Mozilla/5.0 (Linux; Android 14) Chrome/140")]
        body = (await web.driver_link_page(req, token, session)).body.decode()
        assert f"intent://d/{token}#Intent;scheme=condur;package=ru.condur.condur;" in body
        assert "manual%3D1" in body            # без приложения — назад без автозапуска
        manual = _request(app, method="GET", path=f"/d/{token}")
        manual.scope["query_string"] = b"manual=1"
        page = (await web.driver_link_page(manual, token, session)).body.decode()
        assert "Открываю приложение" not in page and "condur://" in page
        await session.close()
    _run(scenario)


def test_android_app_links_файл_подписи():
    async def scenario():
        response = await web.android_asset_links()
        data = json.loads(response.body)
        target = data[0]["target"]
        assert target["package_name"] == "ru.condur.condur"
        fingerprint = target["sha256_cert_fingerprints"][0]
        assert re.fullmatch(r"([0-9A-F]{2}:){31}[0-9A-F]{2}", fingerprint)
    _run(scenario)


def test_одновременный_дубль_через_адрес_не_роняет_сервер(monkeypatch):
    """Два одинаковых запроса почти разом: второй откатывается и отдаёт ответ
    первого. После отката нельзя трогать объекты сессии — это роняло бы запрос."""
    from app.services import driver_actions_service as actions

    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        code, _, _ = await _issue_code(app, session, owner, driver, owner_cookie)
        cookie = _cookie(await _redeem(app, session, code=code), "driver_session")
        ctx = await _driver_ctx(app, session, cookie)
        body = {"client_op_id": "race-through-api", "type": "shift.start",
                "payload": {"vehicle_id": vehicle.id}}
        first = _json(await web.api_driver_actions(
            _request(app, cookie=cookie, body=body), ctx, session))

        real_find = actions.find_existing
        calls = {"n": 0}

        async def blind_once(*args, **kwargs):
            calls["n"] += 1
            return None if calls["n"] == 1 else await real_find(*args, **kwargs)

        monkeypatch.setattr(actions, "find_existing", blind_once)
        second = _json(await web.api_driver_actions(
            _request(app, cookie=cookie, body=body), ctx, session))
        assert second["duplicate"] is True
        assert second["ok"] is True and second["shift_id"] == first["shift_id"]
        await session.close()
    _run(scenario)


def test_вход_водителя_виден_в_журнале():
    """Владелец 12.09.2026: «я же их должен видеть»."""
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        code, _, _ = await _issue_code(app, session, owner, driver, owner_cookie)
        await _redeem(app, session, code=code)
        feed = await web.api_events(owner, session)
        rows = [e for e in feed["events"] if e["type"] == "driver_app_login"]
        assert len(rows) == 1
        assert rows[0]["label"] == "Вошёл в приложение"
        assert rows[0]["detail"] == "с iPhone 15"
        assert rows[0]["driver"] == "Саломов Холбек"
        # Выдача доступа — действие владельца, в ленту не попадает.
        assert not [e for e in feed["events"] if e["type"] == "driver_access_issued"]
        await session.close()
    _run(scenario)


def test_страница_водителей_рисуется_с_кнопкой_приложения():
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, owner_cookie = await _db()
        code, _, _ = await _issue_code(app, session, owner, driver, owner_cookie)
        await _redeem(app, session, code=code)
        page = await web.drivers_page(
            _request(app, method="GET", path="/drivers", cookie=owner_cookie),
            owner, session,
        )
        html = page.body.decode()
        assert f'hx-get="/drivers/{driver.id}/app"' in html
        assert "📱 1" in html                 # телефон с приложением
        assert "не активирован" not in html   # вошёл через приложение — активен
        assert "function drvCopy" in html
        await session.close()
    _run(scenario)
