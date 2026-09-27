"""Суточные по дням владельца и доставка сообщений (разбор 27.09.2026)."""
import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")

from aiogram.exceptions import TelegramNetworkError  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402

from app.bots import notifications as N  # noqa: E402
from app.services import salary_service  # noqa: E402

UTC = timezone.utc


def _shift(start: datetime, end: datetime):
    return SimpleNamespace(started_at=start, ended_at=end, distance_km=None)


def _driver(per_diem: str = "1000"):
    return SimpleNamespace(
        salary_type="fixed_per_shift", salary_rate=Decimal(0), per_diem_rub=Decimal(per_diem)
    )


def test_ночная_смена_москвы_после_полуночи_одни_сутки():
    # 01:00–10:00 по Москве = 22:00 вчера – 07:00 по UTC. По UTC было двое суток.
    shift = _shift(datetime(2026, 9, 25, 22, 0, tzinfo=UTC), datetime(2026, 9, 26, 7, 0, tzinfo=UTC))
    assert salary_service._count_days(shift, "Europe/Moscow") == 1
    assert salary_service.calculate_salary(_driver(), shift, [], "Europe/Moscow") == Decimal("1000.00")


def test_смена_через_полночь_по_москве_двое_суток():
    # 20:00–02:00 по Москве = 17:00–23:00 по UTC. По UTC были одни сутки.
    shift = _shift(datetime(2026, 9, 25, 17, 0, tzinfo=UTC), datetime(2026, 9, 25, 23, 0, tzinfo=UTC))
    assert salary_service._count_days(shift, "Europe/Moscow") == 2


def test_сибирь_считает_по_своим_часам():
    # 23:00 31.01 – 08:00 01.02 по Новосибирску (UTC+7) — два календарных дня.
    shift = _shift(datetime(2026, 1, 31, 16, 0, tzinfo=UTC), datetime(2026, 2, 1, 1, 0, tzinfo=UTC))
    assert salary_service._count_days(shift, "Asia/Novosibirsk") == 2
    # Дневная смена 09:00–18:00 там же — одни сутки.
    day = _shift(datetime(2026, 2, 1, 2, 0, tzinfo=UTC), datetime(2026, 2, 1, 11, 0, tzinfo=UTC))
    assert salary_service._count_days(day, "Asia/Novosibirsk") == 1


def test_длинное_сообщение_режется_по_строкам():
    line = "🚚 <b>А123ВС 77</b> — 12 рейсов, 640 км, выручка 180 000 ₽"
    text = "\n".join(f"{i}. {line}" for i in range(200))
    assert len(text) > N.TELEGRAM_TEXT_MAX
    parts = N.split_long(text)
    assert len(parts) > 1
    assert all(len(p) <= N.TELEGRAM_TEXT_MAX for p in parts)
    # Ни одна строка не разрезана — теги целы, ничего не потеряно.
    assert "\n".join(parts) == text
    assert all(p.count("<b>") == p.count("</b>") for p in parts)
    assert N.split_long("коротко") == ["коротко"]


class _Bot:
    """Бот, у которого можно «уронить сеть» на первые N отправок."""

    def __init__(self, fail: int = 0):
        self.fail = fail
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            self.fail -= 1
            raise TelegramNetworkError(method=SendMessage(chat_id=chat_id, text=text), message="timeout")
        self.sent.append((chat_id, text, reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


def test_водителю_сбой_сети_повторяется_а_не_роняет_страницу(monkeypatch):
    async def fast(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "sleep", fast)
    bot = _Bot(fail=1)
    assert asyncio.run(N.notify_driver(bot, None, 42, "Расход одобрен")) is True
    assert bot.sent == [(42, "Расход одобрен", None)]

    # Сеть лежит все три попытки — False, а не исключение наверх.
    dead = _Bot(fail=10)
    assert asyncio.run(N.notify_driver(dead, None, 42, "Расход одобрен")) is False


def test_кнопки_у_последней_части_длинного_сообщения():
    bot = _Bot()
    markup = object()
    text = "\n".join("строка сводки " * 20 for _ in range(40))
    sent = asyncio.run(N._send_text(bot, 7, text, markup))
    assert len(bot.sent) > 1
    assert [m for _, _, m in bot.sent] == [None] * (len(bot.sent) - 1) + [markup]
    assert sent.message_id == len(bot.sent)
