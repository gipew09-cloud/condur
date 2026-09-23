"""Фото из приложения водителя и проверка одометра.

Случай владельца 17.09.2026, смена 119: водитель прислал в конце смены то же
фото одометра, что утром. Распознавание прочитало то же число, пробег вышел
нулём, владельцу никто ничего не сказал, а пробега по GPS на странице смены не
было. Здесь закреплено: фото из приложения доходят, хранятся у нас, чужие не
видны; одно и то же фото и одинаковый одометр замечаются; пробег по GPS виден.
"""
import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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

from app.bots import driver_bot  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import (  # noqa: E402
    Base, Driver, DriverPhoto, Expense, Owner, RouteTemplate, Shift, Trip, TripDocument, Vehicle,
    VehicleTelemetryPoint, WebSession,
)
from app.services import auth_service, driver_photos, odometer_check, shift_flow  # noqa: E402
from app.services.receipt_ocr import OdometerReading  # noqa: E402
from app.web import router as web  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


_ENGINES = []

JPEG = b"\xff\xd8\xff\xe0" + b"odometer-959141" * 20
JPEG_2 = b"\xff\xd8\xff\xe0" + b"odometer-959230" * 20


def _run(scenario):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        while _ENGINES:
            loop.run_until_complete(_ENGINES.pop().dispose())
        loop.close()


class _App:
    def __init__(self):
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(message_id=1)
        bot.send_photo.return_value = SimpleNamespace(message_id=2)
        self.state = SimpleNamespace(owner_bot=bot, driver_bot=AsyncMock())


def _request(app, *, method="POST", path="/", cookie=None, body=None,
             raw=None, content_type=None, agent=b"Condur App (ios)", query=b""):
    headers = [(b"user-agent", agent)]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    if body is not None:
        raw = json.dumps(body).encode()
        content_type = "application/json"
    raw = raw or b""
    if content_type:
        headers.append((b"content-type", content_type.encode()))
    headers.append((b"content-length", str(len(raw)).encode()))

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request({
        "type": "http", "method": method, "path": path, "headers": headers,
        "query_string": query, "scheme": "http", "server": ("test", 80),
        "client": ("10.0.0.1", 5555), "app": app,
    }, receive)


def _multipart(fields: dict, data: bytes, filename="odo.jpg", ctype="image/jpeg"):
    boundary = "----condur-test-boundary"
    out = []
    for key, value in fields.items():
        out.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            .encode()
        )
    out.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode() + data + b"\r\n"
    )
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def _json(response):
    return json.loads(response.body)


def _cookie(response, name):
    for key, value in response.raw_headers:
        if key == b"set-cookie" and value.decode().startswith(name + "="):
            return value.decode().split(";")[0]
    return None


def _teach_timezone(engine):
    """Кабинет считает дни в поясе владельца функцией Postgres timezone().
    SQLite её не знает — учим, как в tests/test_finance_ledger.py."""
    from sqlalchemy import event
    from zoneinfo import ZoneInfo

    @event.listens_for(engine.sync_engine, "connect")
    def _teach(conn, _):
        def tz(zone, moment):
            if moment is None:
                return None
            when = datetime.fromisoformat(str(moment))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.astimezone(ZoneInfo(str(zone))).strftime("%Y-%m-%d %H:%M:%S")
        conn.create_function("timezone", 2, tz)


async def _db():
    engine = create_async_engine("sqlite+aiosqlite://")
    _teach_timezone(engine)
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    owner = Owner(telegram_id=111, full_name="Кибиткина", company_name="ИП Кибиткина",
                  timezone="Europe/Moscow", notifications_enabled=True)
    session.add(owner)
    await session.flush()
    vehicle = Vehicle(owner_id=owner.id, license_plate="Т772НХ178", is_active=True)
    driver = Driver(owner_id=owner.id, full_name="Саломов Холбек",
                    salary_type="per_km", salary_rate=10, is_active=True)
    session.add_all([vehicle, driver])
    await session.flush()
    session.add(RouteTemplate(owner_id=owner.id, name="Л", origin="Агропарк",
                              destination="Лента", is_active=True))
    owner_token = auth_service.new_session_token()
    session.add(WebSession(
        owner_id=owner.id, telegram_id=owner.telegram_id,
        token_hash=auth_service.session_token_hash(owner_token),
    ))
    await session.commit()
    return session, owner, vehicle, driver, maker_of(engine)


def maker_of(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def _signed_in(app, session, owner, driver, device="phone-1"):
    from app.services import driver_access_service as access
    issued = await access.issue_grant(session, driver=driver, issued_by_telegram_id=111)
    await session.commit()
    req = _request(app, path="/api/driver/redeem", body={
        "code": issued.code, "device_id": device, "device_label": "iPhone 14",
        "platform": "ios", "app_version": "0.3.0",
    })
    response = await web.api_driver_redeem(req, session)
    cookie = _cookie(response, "driver_session")
    ctx = await web.current_driver(_request(app, method="GET", cookie=cookie), session)
    return cookie, ctx


async def _upload(app, session, ctx, cookie, *, client_id, data=JPEG, kind="odometer_start",
                  source=None):
    # Как на живом сервере: у каждого запроса свой вход (после отказа сессия
    # откатывается, и прежние объекты протухают).
    ctx = await web.current_driver(_request(app, method="GET", cookie=cookie), session)
    fields = {"client_id": client_id, "kind": kind, "taken_at": ""}
    if source is not None:
        fields["source"] = source
    raw, ctype = _multipart(fields, data)
    req = _request(app, path="/api/driver/photos", cookie=cookie, raw=raw, content_type=ctype)
    return await web.api_driver_photo_upload(req, ctx, session)


async def _action(app, session, ctx, cookie, op_id, kind, payload):
    req = _request(app, path="/api/driver/actions", cookie=cookie, body={
        "client_op_id": op_id, "type": kind, "payload": payload,
    })
    return _json(await web.api_driver_actions(req, ctx, session))


# ------------------------------------------------------------------ приём
def test_фото_принимается_один_раз_и_только_фото(monkeypatch):
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, _ = await _db()
        cookie, ctx = await _signed_in(app, session, owner, driver)

        first = _json(await _upload(app, session, ctx, cookie, client_id="photo-0001"))
        assert first["ok"] is True and first["photo"].startswith("app-")
        again = _json(await _upload(app, session, ctx, cookie, client_id="photo-0001"))
        assert again["photo"] == first["photo"] and again["duplicate"] is True
        assert len((await session.execute(select(DriverPhoto))).scalars().all()) == 1

        html = await _upload(app, session, ctx, cookie, client_id="photo-0002",
                             data=b"<html><script>alert(1)</script></html>")
        assert html.status_code == 400 and "не похоже на фото" in _json(html)["message"]

        bad_kind = await _upload(app, session, ctx, cookie, client_id="photo-0003",
                                 kind="selfie")
        assert bad_kind.status_code == 400

        monkeypatch.setattr(driver_photos, "MAX_BYTES", 100)
        big = await _upload(app, session, ctx, cookie, client_id="photo-0004")
        assert big.status_code == 413 or "большое" in _json(big)["message"]
        await session.close()
    _run(scenario)


def test_без_фото_одометра_смену_не_начать_как_в_боте(monkeypatch):
    monkeypatch.setattr(settings, "feature_odometer_photo", True)

    async def scenario():
        app = _App()
        session, owner, vehicle, driver, _ = await _db()
        cookie, ctx = await _signed_in(app, session, owner, driver)
        no_photo = await _action(app, session, ctx, cookie, "op-start-001", "shift.start",
                                 {"vehicle_id": vehicle.id})
        assert no_photo["status"] == "rejected"
        assert "Сфотографируйте одометр" in no_photo["message"]

        photo = _json(await _upload(app, session, ctx, cookie, client_id="photo-0001"))["photo"]
        started = await _action(app, session, ctx, cookie, "op-start-002", "shift.start",
                                {"vehicle_id": vehicle.id, "photo": photo})
        assert started["ok"] is True
        shift = (await session.execute(select(Shift))).scalar_one()
        assert shift.odometer_start_photo_url == photo
        # Владельцу — фото с кнопкой «Указать пробег», как от бота.
        call = app.state.owner_bot.send_photo.await_args_list[-1]
        assert "Фото одометра в начале смены" in call.kwargs["caption"]
        assert "odo:start:" in call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        await session.close()
    _run(scenario)


def test_чужое_фото_не_принимается(monkeypatch):
    monkeypatch.setattr(settings, "feature_odometer_photo", True)

    async def scenario():
        app = _App()
        session, owner, vehicle, driver, _ = await _db()
        other = Driver(owner_id=owner.id, full_name="Бахман", is_active=True)
        session.add(other)
        await session.commit()
        cookie, ctx = await _signed_in(app, session, owner, driver)
        cookie2, ctx2 = await _signed_in(app, session, owner, other, device="phone-2")
        stranger = _json(await _upload(app, session, ctx2, cookie2, client_id="photo-0009"))
        out = await _action(app, session, ctx, cookie, "op-start-001", "shift.start",
                            {"vehicle_id": vehicle.id, "photo": stranger["photo"]})
        assert out["status"] == "rejected" and "не дошло" in out["message"]
        await session.close()
    _run(scenario)


def test_то_же_фото_вечером_замечается(monkeypatch):
    """Смена 119: одно и то же фото в начале и в конце."""
    monkeypatch.setattr(settings, "feature_odometer_photo", True)
    checks = []

    async def fake_followup(**kwargs):
        checks.append(kwargs)

    monkeypatch.setattr(odometer_check, "followup", fake_followup)

    async def scenario():
        app = _App()
        session, owner, vehicle, driver, _ = await _db()
        cookie, ctx = await _signed_in(app, session, owner, driver)
        morning = _json(await _upload(app, session, ctx, cookie, client_id="photo-0001"))["photo"]
        await _action(app, session, ctx, cookie, "op-start-001", "shift.start",
                      {"vehicle_id": vehicle.id, "photo": morning})
        # Вечером — то же самое фото, отправленное заново (другой номер).
        evening = _json(await _upload(app, session, ctx, cookie, client_id="photo-0002",
                                      kind="odometer_end"))["photo"]
        assert evening != morning
        finished = await _action(app, session, ctx, cookie, "op-finish-01", "shift.finish",
                                 {"photo": evening})
        assert finished["ok"] is True
        await asyncio.sleep(0)
        assert checks and checks[-1]["same_photo_as_start"] is True
        assert checks[-1]["closing"] is True
        # На странице смены — предупреждение.
        shift = (await session.execute(select(Shift))).scalar_one()
        page = await web.shift_detail(_request(app, method="GET"), shift.id, owner, session)
        assert "одно и то же" in page.body.decode()

        # Другое фото — без тревоги.
        checks.clear()
        await _action(app, session, ctx, cookie, "op-start-002", "shift.start",
                      {"vehicle_id": vehicle.id, "photo": morning})
        other = _json(await _upload(app, session, ctx, cookie, client_id="photo-0003",
                                    data=JPEG_2, kind="odometer_end"))["photo"]
        await _action(app, session, ctx, cookie, "op-finish-02", "shift.finish",
                      {"photo": other})
        await asyncio.sleep(0)
        assert not checks, "OCR выключен, фото разные — проверять нечего"
        await session.close()
    _run(scenario)


def test_фото_из_приложения_видно_только_своему_владельцу():
    async def scenario():
        app = _App()
        session, owner, vehicle, driver, _ = await _db()
        cookie, ctx = await _signed_in(app, session, owner, driver)
        ref = _json(await _upload(app, session, ctx, cookie, client_id="photo-0001"))["photo"]
        response = await web.api_photo(_request(app, method="GET"), ref, owner, session)
        assert response.body == JPEG
        assert response.media_type == "image/jpeg"
        stranger = Owner(telegram_id=999, full_name="Чужой", timezone="Europe/Moscow")
        session.add(stranger)
        await session.commit()
        with pytest.raises(HTTPException) as denied:
            await web.api_photo(_request(app, method="GET"), ref, stranger, session)
        assert denied.value.status_code == 403
        await session.close()
    _run(scenario)


def test_фото_ттн_к_рейсу(monkeypatch):
    monkeypatch.setattr(settings, "feature_odometer_photo", False)

    async def scenario():
        app = _App()
        session, owner, vehicle, driver, _ = await _db()
        cookie, ctx = await _signed_in(app, session, owner, driver)
        await _action(app, session, ctx, cookie, "op-start-001", "shift.start",
                      {"vehicle_id": vehicle.id})
        route = (await session.execute(select(RouteTemplate))).scalar_one()
        await _action(app, session, ctx, cookie, "op-trip-0001", "trip.create",
                      {"template_id": route.id})
        ref = _json(await _upload(app, session, ctx, cookie, client_id="photo-0005",
                                  kind="waybill"))["photo"]
        out = await _action(app, session, ctx, cookie, "op-ttn-0001", "trip.waybill",
                            {"photo": ref})
        assert out["ok"] is True
        trip = (await session.execute(select(Trip))).scalar_one()
        assert trip.waybill_photo_url == ref
        call = app.state.owner_bot.send_photo.await_args_list[-1]
        assert "ТТН от" in call.kwargs["caption"]
        await session.close()
    _run(scenario)


def test_расход_с_чеком_и_sos_через_адреса_приложения(monkeypatch):
    """Путь телефона целиком (17.09.2026): чек → расход → владельцу фото с
    кнопками «Одобрить / Изменить / Отклонить»; SOS → сообщение владельцу.
    Фото одометра видно в журнале владельца."""
    monkeypatch.setattr(settings, "feature_odometer_photo", True)
    from app.services import receipt_ocr
    monkeypatch.setattr(receipt_ocr, "is_enabled", lambda: False)

    async def scenario():
        app = _App()
        session, owner, vehicle, driver, _ = await _db()
        cookie, ctx = await _signed_in(app, session, owner, driver)
        odo = _json(await _upload(app, session, ctx, cookie, client_id="photo-0101"))["photo"]
        started = await _action(app, session, ctx, cookie, "op-start-101", "shift.start",
                                {"vehicle_id": vehicle.id, "photo": odo})
        assert started["ok"] is True

        receipt = _json(await _upload(app, session, ctx, cookie, client_id="photo-0102",
                                      kind="receipt"))["photo"]
        ctx = await web.current_driver(_request(app, method="GET", cookie=cookie), session)
        out = await _action(app, session, ctx, cookie, "op-exp-0101", "expense.create",
                            {"category": "fuel", "amount": "3 200", "photo": receipt})
        assert out["ok"] is True and "expense_id" in out
        expense = (await session.execute(select(Expense))).scalar_one()
        assert expense.receipt_photo_url == receipt and expense.status == "pending"
        call = app.state.owner_bot.send_photo.await_args_list[-1]
        assert "Расход от <b>Саломов Холбек</b>" in call.kwargs["caption"]
        buttons = call.kwargs["reply_markup"].inline_keyboard[0]
        assert [b.callback_data for b in buttons] == [
            f"expense:approve:{expense.id}", f"expense:edit:{expense.id}",
            f"expense:reject:{expense.id}",
        ]

        ctx = await web.current_driver(_request(app, method="GET", cookie=cookie), session)
        sos = await _action(app, session, ctx, cookie, "op-sos-0101", "sos.send", {})
        assert sos["ok"] is True
        text = app.state.owner_bot.send_message.await_args_list[-1].args[1]
        assert "SOS" in text and "Т772НХ178" in text

        ctx = await web.current_driver(_request(app, method="GET", cookie=cookie), session)
        me = await web.api_driver_me(ctx, session)
        assert me["sos"] is True
        # Цвет кузова — для той же машинки, что у владельца; не выбран — чёрная.
        assert me["shift"]["color"] == "black"
        assert me["vehicles"][0]["color"] == "black"
        assert [c["code"] for c in me["expense_categories"]][:2] == ["fuel", "repair"]

        feed = await web.api_events(owner, session)
        rows = {e["type"]: e for e in feed["events"]}
        assert rows["shift_started"]["photo"] == odo
        assert rows["expense_submitted"]["photo"] == receipt
        assert rows["sos"]["label"]
        # Владелец открывает эти фото по тем же адресам, что и фото из бота.
        assert await web._owner_owns_photo(session, owner.id, odo)
        assert await web._owner_owns_photo(session, owner.id, receipt)
        await session.close()
    _run(scenario)


def test_владелец_видит_откуда_фото_камера_или_галерея(monkeypatch):
    """Владелец 17.09.2026: «нужно для владельца добавить, откуда фотка была —
    из камеры или галереи». Везде, где он видит фото из приложения: подпись в
    Telegram, журнал в приложении, страницы кабинета."""
    monkeypatch.setattr(settings, "feature_odometer_photo", True)
    from app.services import receipt_ocr
    monkeypatch.setattr(receipt_ocr, "is_enabled", lambda: False)

    async def scenario():
        app = _App()
        session, owner, vehicle, driver, _ = await _db()
        cookie, ctx = await _signed_in(app, session, owner, driver)
        odo = _json(await _upload(app, session, ctx, cookie, client_id="photo-0201",
                                  source="camera"))["photo"]
        receipt = _json(await _upload(app, session, ctx, cookie, client_id="photo-0202",
                                      kind="receipt", source="gallery"))["photo"]
        odd = _json(await _upload(app, session, ctx, cookie, client_id="photo-0203",
                                  kind="receipt", source="<b>scanner</b>"))["photo"]
        old_app = _json(await _upload(app, session, ctx, cookie, client_id="photo-0204",
                                      kind="receipt"))["photo"]
        sources = {
            f"app-{p.id}": p.source
            for p in (await session.execute(select(DriverPhoto))).scalars().all()
        }
        assert sources == {odo: "camera", receipt: "gallery", odd: None, old_app: None}

        await _action(app, session, ctx, cookie, "op-start-201", "shift.start",
                      {"vehicle_id": vehicle.id, "photo": odo})
        caption = app.state.owner_bot.send_photo.await_args_list[-1].kwargs["caption"]
        assert caption.endswith("📷 Снято камерой")
        await _action(app, session, ctx, cookie, "op-exp-0201", "expense.create",
                      {"category": "fuel", "amount": "900", "photo": receipt})
        caption = app.state.owner_bot.send_photo.await_args_list[-1].kwargs["caption"]
        assert caption.endswith("🖼 Из галереи — могло быть снято раньше")
        await _action(app, session, ctx, cookie, "op-exp-0202", "expense.create",
                      {"category": "repair", "amount": "100", "photo": old_app})
        caption = app.state.owner_bot.send_photo.await_args_list[-1].kwargs["caption"]
        assert "камер" not in caption and "галере" not in caption

        rows = {e["type"] + str(e["photo"]): e
                for e in (await web.api_events(owner, session))["events"]}
        assert rows["shift_started" + odo]["photo_source"] == "camera"
        assert rows["expense_submitted" + receipt]["photo_source"] == "gallery"
        assert rows["expense_submitted" + old_app]["photo_source"] is None

        shift = (await session.execute(select(Shift))).scalar_one()
        page = (await web.shift_detail(
            _request(app, method="GET"), shift.id, owner, session,
        )).body.decode()
        assert "📷 камера" in page and "🖼 галерея" in page
        # Список трат с 23.09.2026 — «Финансы → Расходы»: отметка там же.
        from app.web import finance_routes
        expenses = (await finance_routes.finance_expenses(
            _request(app, method="GET", path="/finances/expenses"), owner, session,
        )).body.decode()
        assert "🖼 галерея" in expenses
        await session.close()
    _run(scenario)


def test_у_фото_из_бота_отметки_нет():
    async def scenario():
        maker, owner_id, shift_id = await _shift_db(
            odometer_start_photo_url="AgACAgIAA-бот",
        )
        async with maker() as session:
            owner = await session.get(Owner, owner_id)
            assert await driver_photos.origins(session, owner_id, ["AgACAgIAA-бот", None]) == {}
            page = (await web.shift_detail(
                _request(_App(), method="GET"), shift_id, owner, session,
            )).body.decode()
        assert "photo-origin" not in page
        assert "/api/photo/AgACAgIAA-бот" in page
    _run(scenario)


# --------------------------------------------------------- проверка одометра
async def _shift_db(**shift_kwargs):
    engine = create_async_engine("sqlite+aiosqlite://")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = maker_of(engine)
    async with maker() as session:
        owner = Owner(telegram_id=1, full_name="Владелец", notifications_enabled=True)
        session.add(owner)
        await session.flush()
        vehicle = Vehicle(owner_id=owner.id, license_plate="Т772НХ178", is_active=True)
        driver = Driver(owner_id=owner.id, full_name="Саломов", telegram_id=2)
        session.add_all([vehicle, driver])
        await session.flush()
        start = datetime(2026, 9, 14, 3, 37, tzinfo=timezone.utc)
        shift = Shift(owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
                      started_at=start, ended_at=start + timedelta(hours=6),
                      status="completed", **shift_kwargs)
        session.add(shift)
        # Трекер проехал 87 км за смену.
        for minutes, km in ((0, 100000), (180, 100040), (360, 100087)):
            session.add(VehicleTelemetryPoint(
                owner_id=owner.id, vehicle_id=vehicle.id,
                observed_at=start + timedelta(minutes=minutes),
                mileage_km=Decimal(km), is_valid=True,
            ))
        await session.commit()
        return maker, owner.id, shift.id


def _notify_into(monkeypatch, maker, told):
    monkeypatch.setattr(odometer_check, "async_session", maker)

    async def _notify(owner_bot, session, owner, text, **kw):
        told.append(text)

    monkeypatch.setattr(odometer_check, "notify_owner", _notify)


def test_то_же_фото_предупреждение_с_пробегом_по_gps(monkeypatch):
    told = []

    async def scenario():
        maker, owner_id, shift_id = await _shift_db(odometer_start=959141)
        _notify_into(monkeypatch, maker, told)
        await odometer_check.followup(
            owner_bot=None, shift_id=shift_id, owner_id=owner_id, driver_name="Саломов",
            plate="Т772НХ178", closing=True, image_bytes=None, same_photo_as_start=True,
        )
    _run(scenario)
    assert len(told) == 1
    assert "то же самое, что в начале" in told[0]
    assert "По GPS за смену: <b>87 км</b>" in told[0]


def test_распознанное_не_больше_утреннего_не_пишется(monkeypatch):
    told = []

    async def scenario():
        maker, owner_id, shift_id = await _shift_db(odometer_start=959141)
        _notify_into(monkeypatch, maker, told)

        async def _same(_bytes):
            return OdometerReading(km=959141)

        monkeypatch.setattr(odometer_check.receipt_ocr, "recognize_odometer", _same)
        await odometer_check.followup(
            owner_bot=None, shift_id=shift_id, owner_id=owner_id, driver_name="Саломов",
            plate="Т772НХ178", closing=True, image_bytes=b"x",
        )
        async with maker() as session:
            shift = await session.get(Shift, shift_id)
            assert shift.odometer_end is None, "пробег 0 записался бы молча"
    _run(scenario)
    assert "не больше, чем в начале" in told[0]
    assert "87 км" in told[0]


def test_распознанное_вписывается_и_сверяется_с_gps(monkeypatch):
    told = []

    async def scenario():
        maker, owner_id, shift_id = await _shift_db(odometer_start=959141)
        _notify_into(monkeypatch, maker, told)

        async def _read(_bytes):
            return OdometerReading(km=959230)

        monkeypatch.setattr(odometer_check.receipt_ocr, "recognize_odometer", _read)
        await odometer_check.followup(
            owner_bot=None, shift_id=shift_id, owner_id=owner_id, driver_name="Саломов",
            plate="Т772НХ178", closing=True, image_bytes=b"x",
        )
        async with maker() as session:
            assert (await session.get(Shift, shift_id)).odometer_end == 959230
    _run(scenario)
    assert "959 230 км" in told[0]
    assert "По GPS" in told[0] and "87 км" in told[0]


def test_бот_узнаёт_то_же_фото_по_telegram(monkeypatch):
    async def scenario():
        maker, owner_id, shift_id = await _shift_db(odometer_start=None)
        async with maker() as session:
            shift = await session.get(Shift, shift_id)
            shift.odometer_start_photo_url = "file-morning"
            await session.commit()
        monkeypatch.setattr(driver_bot, "async_session", maker)

        class _Bot:
            async def get_file(self, file_id):
                return SimpleNamespace(file_unique_id="same-picture")

            async def download(self, file_id):
                import io
                return io.BytesIO(b"x")

        assert await driver_bot._same_photo_as_start(_Bot(), shift_id, "file-evening")
        assert await driver_bot._same_photo_as_start(_Bot(), shift_id, "file-morning")

        class _Other(_Bot):
            async def get_file(self, file_id):
                return SimpleNamespace(file_unique_id=file_id)

            async def download(self, file_id):
                import io
                return io.BytesIO(file_id.encode())

        assert not await driver_bot._same_photo_as_start(_Other(), shift_id, "file-evening")
    _run(scenario)


def test_итог_смены_с_одинаковым_одометром_и_gps():
    shift = SimpleNamespace(odometer_start=959141, odometer_end=959141, distance_km=0)
    closed = SimpleNamespace(
        shift=shift, trips=[], revenue=Decimal(0), pending_revenue=Decimal(0),
        expenses_total=Decimal(0), salary=Decimal(0), gps_km=Decimal(87),
        ignition=None, ended_at=datetime.now(timezone.utc),
    )
    driver = SimpleNamespace(full_name="Саломов")
    text = shift_flow.shift_completed_owner_text(closed, driver=driver, tz_name=None)
    assert "одинаковый" in text and "87 км" in text

    shift.odometer_end, shift.distance_km = 959230, 89
    text = shift_flow.shift_completed_owner_text(closed, driver=driver, tz_name=None)
    assert "одинаковый" not in text
    assert "По GPS: 87 км" in text


def test_страница_смены_показывает_пробег_по_gps():
    async def scenario():
        maker, owner_id, shift_id = await _shift_db(odometer_start=959141, odometer_end=959141)
        async with maker() as session:
            owner = await session.get(Owner, owner_id)
            owner.timezone = "Europe/Moscow"
            page = await web.shift_detail(
                _request(_App(), method="GET"), shift_id, owner, session,
            )
            html = page.body.decode()
        assert "Пробег · по GPS" in html and "87 км" in html
        assert "одинаковый" in html
    _run(scenario)


# ------------------------------------------------ документы с сайта (аудит)
def test_загруженная_страница_не_откроется_на_нашем_сайте():
    async def scenario():
        maker, owner_id, shift_id = await _shift_db()
        async with maker() as session:
            owner = await session.get(Owner, owner_id)
            trip = Trip(owner_id=owner_id, shift_id=shift_id, driver_id=1, vehicle_id=1)
            session.add(trip)
            await session.flush()
            evil = TripDocument(trip_id=trip.id, owner_id=owner_id, filename="счёт.html",
                                content_type="text/html",
                                data=b"<html><script>steal()</script></html>")
            pdf = TripDocument(trip_id=trip.id, owner_id=owner_id, filename="ttn.pdf",
                               content_type="application/pdf", data=b"%PDF-1.7 ...")
            session.add_all([evil, pdf])
            await session.commit()
            bad = await web.get_trip_document(evil.id, owner, session)
            assert bad.media_type == "application/octet-stream"
            assert bad.headers["content-disposition"].startswith("attachment")
            assert "sandbox" in bad.headers["content-security-policy"]
            good = await web.get_trip_document(pdf.id, owner, session)
            assert good.media_type == "application/pdf"
            assert good.headers["content-disposition"].startswith("inline")
    _run(scenario)


def test_тип_документа_по_содержимому():
    assert web._sniff_document(b"%PDF-1.4") == "application/pdf"
    assert web._sniff_document(JPEG) == "image/jpeg"
    assert web._sniff_document(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert web._sniff_document(b"<svg onload=alert(1)>") == "application/octet-stream"
    assert re.search("attachment", web._document_response(b"MZ...", "a.exe").headers[
        "content-disposition"])
