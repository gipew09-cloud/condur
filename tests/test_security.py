"""
Тесты безопасности: закрытые уязвимости не должны вернуться.

Проверяем три вещи, найденные при аудите:
  1) код входа нельзя подобрать перебором (лимит попыток + сравнение
     постоянного времени);
  2) в подсказки на дашборде нельзя протащить скрипт через госномер или
     ФИО водителя (они выводятся без экранирования, ради тегов <b>);
  3) порт приёма GPS не пускает чужие трекеры, когда задан пароль.

Запуск: pytest tests/test_security.py
"""
import os

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test-secret-please-set-a-long-one-in-prod")

from app.services import auth_service as AU  # noqa: E402


# ------------------------------------------------- 1. перебор кода входа
def test_login_code_burns_after_several_wrong_attempts():
    """6-значный код — это миллион вариантов, а Telegram ID не секрет.
    Без лимита его можно было подбирать. После MAX_CODE_ATTEMPTS промахов
    код сгорает: нужно заново просить /login у бота."""
    tg = 555001
    code = AU.issue_code(tg)

    for _ in range(AU.MAX_CODE_ATTEMPTS):
        assert AU.consume_code(tg, "000000" if code != "000000" else "111111") is False
    # код сгорел — даже ПРАВИЛЬНЫЙ больше не подходит
    assert AU.consume_code(tg, code) is False


def test_login_code_works_after_a_couple_of_typos():
    """Опечатки живого человека не должны сжигать код раньше времени."""
    tg = 555002
    code = AU.issue_code(tg)
    wrong = "000000" if code != "000000" else "111111"
    assert AU.consume_code(tg, wrong) is False
    assert AU.consume_code(tg, wrong) is False
    assert AU.consume_code(tg, code) is True        # третья попытка — верная
    assert AU.consume_code(tg, code) is False       # и код одноразовый


def test_login_code_compared_in_constant_time():
    """Сравнение через secrets.compare_digest: по времени ответа нельзя
    подбирать код посимвольно."""
    import inspect
    src = inspect.getsource(AU.consume_code)
    assert "compare_digest" in src
    assert "entry.code != code" not in src, "вернулось обычное сравнение строк"


# ------------------------------------------------- 2. XSS в подсказках
def test_insights_escape_user_typed_values():
    """Госномер и ФИО вводит человек, а подсказки выводятся без экранирования
    (нужны теги <b>). Значит экранировать обязаны мы сами — иначе админ
    кабинета мог бы вписать скрипт, и тот выполнился бы у владельца."""
    from app.web.insights import _safe

    assert _safe("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert _safe('" onerror="x') == "&#34; onerror=&#34;x"
    assert _safe(None) == ""
    assert _safe("Т557ОС178") == "Т557ОС178"        # обычный номер не портим


def test_all_insight_strings_escape_dynamic_values():
    """Каждая подсказка с госномером/именем обязана прогонять их через _safe."""
    import re
    src = open("app/web/insights.py", encoding="utf-8").read()
    risky = re.findall(r"\{(plate|name)\}", src)
    assert not risky, f"в подсказки подставляется без экранирования: {risky}"
    assert src.count("_safe(plate)") >= 3
    assert "_safe(name)" in src


# ------------------------------------------- 3. чужие трекеры на GPS-порту
def test_gps_port_rejects_wrong_password(monkeypatch):
    """Порт приёма открыт в интернет. Если пароль задан — чужой трекер,
    знающий адрес и ID машины, не должен пролезть."""
    from app.telemetry.egts_receiver import _wialon_login_ok

    monkeypatch.setenv("TELEMETRY_PASSWORD", "s3cret")
    assert _wialon_login_ok("s3cret") is True
    assert _wialon_login_ok("wrong") is False
    assert _wialon_login_ok("") is False
    assert _wialon_login_ok(None) is False


def test_gps_port_stays_open_when_password_not_configured(monkeypatch):
    """Пароль не настроен — работаем как раньше, чтобы не оборвать связь
    с уже настроенными трекерами (иначе включение защиты = простой автопарка)."""
    from app.telemetry.egts_receiver import _wialon_login_ok

    monkeypatch.delenv("TELEMETRY_PASSWORD", raising=False)
    assert _wialon_login_ok(None) is True
    assert _wialon_login_ok("что угодно") is True


def test_wialon_login_parses_password_both_versions():
    """Пароль читается и из версии 1.1, и из 2.0; «NA» = пароля нет."""
    from app.telemetry.wialon import parse_message

    m = parse_message("#L#128464;s3cret")
    assert m.terminal_id == "128464" and m.password == "s3cret"

    m = parse_message("#L#2.0;128464;s3cret;CRC")
    assert m.terminal_id == "128464" and m.password == "s3cret"

    m = parse_message("#L#128464;NA")
    assert m.terminal_id == "128464" and m.password is None


def test_receiver_closes_connection_on_bad_password():
    """Неверный пароль обязан рвать соединение, а не просто игнорироваться."""
    src = open("app/telemetry/egts_receiver.py", encoding="utf-8").read()
    assert "_wialon_login_ok(message.password)" in src
    i = src.index("_wialon_login_ok(message.password)")
    tail = src[i:i + 400]
    assert "return processed" in tail, "соединение не закрывается при неверном пароле"


# ------------------------------- две учётки на одной машине (не уязвимость,
# но приводило к сообщению не тому человеку)
pytest.importorskip("aiosqlite")
pytest.importorskip("aiogram")

import asyncio  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

from app.models import Base, Driver, Owner, Shift, Vehicle  # noqa: E402
from app.services.scheduler_jobs import _remind_drivers_to_start_shift  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


def test_reminder_goes_to_the_driver_who_actually_drives_that_vehicle():
    """За машиной закреплены двое (реальный водитель и тестовый аккаунт
    владельца). Напоминание «начните смену» одно на машину — и уйти оно должно
    тому, кто последним реально на ней ездил, а не случайному из двух."""
    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with sessionmaker() as session:
            owner = Owner(telegram_id=1, full_name="Владелец")
            session.add(owner)
            await session.flush()
            veh = Vehicle(owner_id=owner.id, license_plate="Т557ОС178")
            session.add(veh)
            await session.flush()
            # тестовая учётка заведена ПЕРВОЙ (меньший id) — раньше выигрывала она
            test_acc = Driver(owner_id=owner.id, telegram_id=1001, full_name="Тест",
                              salary_type="per_km", salary_rate=Decimal(0),
                              default_vehicle_id=veh.id)
            real = Driver(owner_id=owner.id, telegram_id=1002, full_name="Саломов",
                          salary_type="per_km", salary_rate=Decimal(0),
                          default_vehicle_id=veh.id)
            session.add_all([test_acc, real])
            await session.flush()
            # реальный водитель ездил на этой машине вчера
            session.add(Shift(owner_id=owner.id, driver_id=real.id, vehicle_id=veh.id,
                              status="completed", started_at=now - timedelta(days=1)))
            await session.commit()

            bot = AsyncMock()
            moving = [(veh.id, "Т557ОС178", Decimal(40), now - timedelta(minutes=10))]
            sent = await _remind_drivers_to_start_shift(
                session, bot, owner, moving, active_driver_ids=set(), now=now
            )
            assert sent is True
            # сообщение ровно одно и адресовано реальному водителю
            assert bot.send_message.await_count == 1
            assert bot.send_message.await_args.args[0] == real.telegram_id
        await engine.dispose()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()
