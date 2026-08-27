"""Распознавание одометра идёт В ФОНЕ и не спорит с владельцем.

Владелец 27.08.2026: «долго приходит ответ от llama, водители не будут долго
ждать после отправки сообщения». Раньше распознавание стояло прямо в
обработчике: водитель отправлял фото одометра и ждал LlamaParse, стоя у
машины. Теперь смена открывается (или закрывается) сразу, а цифра догоняет
владельца отдельным сообщением.

Второе правило, которое здесь закрепляется: **распознанное не затирает
вписанное руками.** Владелец успел поставить пробег — машина молчит. Иначе
OCR подменит верную цифру правдоподобной неверной, и никто не заметит.
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

from app.bots import driver_bot as db  # noqa: E402
from app.models import Base, Driver, Owner, Shift, Vehicle  # noqa: E402
from app.services.receipt_ocr import OdometerReading  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


class _FakeBot:
    """Телеграм-бот, которого здесь нет. Фото отдаём байтами."""

    def __init__(self):
        self.sent = []

    async def download(self, file_id):
        import io as _io
        return _io.BytesIO(b"photo")


def _run(scenario):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(scenario())
    finally:
        loop.close()


async def _db_with_shift(**shift_kwargs):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        owner = Owner(telegram_id=1, full_name="Владелец")
        session.add(owner)
        await session.flush()
        vehicle = Vehicle(owner_id=owner.id, license_plate="Т557ОС178", is_active=True)
        driver = Driver(owner_id=owner.id, full_name="Саломов", telegram_id=2)
        session.add_all([vehicle, driver])
        await session.flush()
        shift = Shift(
            owner_id=owner.id, driver_id=driver.id, vehicle_id=vehicle.id,
            **shift_kwargs,
        )
        session.add(shift)
        await session.commit()
        return maker, owner.id, shift.id


def _patch(monkeypatch, maker, *, km, notified):
    monkeypatch.setattr(db, "async_session", maker)

    async def _recognize(_bytes):
        return OdometerReading(km=km) if km is not None else None

    monkeypatch.setattr(db.receipt_ocr, "recognize_odometer", _recognize)

    async def _notify(owner_bot, session, owner, text, **kw):
        notified.append(text)

    monkeypatch.setattr(db, "notify_owner", _notify)


def test_recognised_odometer_is_written_and_owner_told(monkeypatch):
    notified = []

    async def scenario():
        maker, owner_id, shift_id = await _db_with_shift(odometer_start=None)
        _patch(monkeypatch, maker, km=123456, notified=notified)
        await db._odometer_followup(
            bot=_FakeBot(), owner_bot=_FakeBot(), file_id="f1", shift_id=shift_id,
            owner_id=owner_id, driver_name="Саломов", plate="Т557ОС178",
            closing=False,
        )
        async with maker() as session:
            shift = await session.get(Shift, shift_id)
            assert shift.odometer_start == 123456

    _run(scenario)
    assert len(notified) == 1
    assert "123 456 км" in notified[0]


def test_owner_typed_value_is_never_overwritten(monkeypatch):
    """Главное правило: человек здесь важнее машины."""
    notified = []

    async def scenario():
        maker, owner_id, shift_id = await _db_with_shift(odometer_start=500000)
        _patch(monkeypatch, maker, km=123456, notified=notified)
        await db._odometer_followup(
            bot=_FakeBot(), owner_bot=_FakeBot(), file_id="f1", shift_id=shift_id,
            owner_id=owner_id, driver_name="Саломов", plate="Т557ОС178",
            closing=False,
        )
        async with maker() as session:
            shift = await session.get(Shift, shift_id)
            assert shift.odometer_start == 500000, "OCR затёр цифру владельца"

    _run(scenario)
    assert notified == [], "владельца дёрнули зря"


def test_closing_shift_fills_the_other_end(monkeypatch):
    notified = []

    async def scenario():
        maker, owner_id, shift_id = await _db_with_shift(odometer_start=123000)
        _patch(monkeypatch, maker, km=123456, notified=notified)
        await db._odometer_followup(
            bot=_FakeBot(), owner_bot=_FakeBot(), file_id="f2", shift_id=shift_id,
            owner_id=owner_id, driver_name="Саломов", plate="Т557ОС178",
            closing=True,
        )
        async with maker() as session:
            shift = await session.get(Shift, shift_id)
            assert shift.odometer_start == 123000
            assert shift.odometer_end == 123456

    _run(scenario)
    assert "в конце смены" in notified[0]


def test_unreadable_photo_changes_nothing_and_says_nothing(monkeypatch):
    """Лучше не распознать, чем распознать неправильно."""
    notified = []

    async def scenario():
        maker, owner_id, shift_id = await _db_with_shift(odometer_start=None)
        _patch(monkeypatch, maker, km=None, notified=notified)
        await db._odometer_followup(
            bot=_FakeBot(), owner_bot=_FakeBot(), file_id="f1", shift_id=shift_id,
            owner_id=owner_id, driver_name="Саломов", plate="Т557ОС178",
            closing=False,
        )
        async with maker() as session:
            shift = await session.get(Shift, shift_id)
            assert shift.odometer_start is None

    _run(scenario)
    assert notified == []


def test_broken_ocr_does_not_break_the_shift(monkeypatch):
    """Сбой распознавания не имеет права всплыть наружу: смена уже закрыта."""
    async def scenario():
        maker, owner_id, shift_id = await _db_with_shift(odometer_start=None)
        monkeypatch.setattr(db, "async_session", maker)

        async def _boom(_bytes):
            raise RuntimeError("LlamaParse не ответил")

        monkeypatch.setattr(db.receipt_ocr, "recognize_odometer", _boom)
        # не бросает наружу — иначе задача упала бы в лог как «unhandled»
        await db._odometer_followup(
            bot=_FakeBot(), owner_bot=_FakeBot(), file_id="f1", shift_id=shift_id,
            owner_id=owner_id, driver_name="Саломов", plate="Т557ОС178",
            closing=False,
        )

    _run(scenario)
