"""
Middleware для inject'а сессии БД и "чужого" бота в каждый хендлер.

Использование:
    async def my_handler(message: Message, session: AsyncSession, owner_bot: Bot): ...

Сессия открывается на каждый апдейт и закрывается после — нет глобальной
переменной, нет утечек, состояние не пересекается между параллельными
апдейтами.
"""
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import async_sessionmaker


class DbSessionMiddleware(BaseMiddleware):
    """Открывает AsyncSession и кладёт в data['session']."""

    def __init__(self, session_factory: async_sessionmaker):
        super().__init__()
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            return await handler(event, data)


class CrossBotMiddleware(BaseMiddleware):
    """
    Прокидывает в хендлеры одного бота инстанс ДРУГОГО бота —
    чтобы хендлеры driver_bot могли слать уведомления через owner_bot,
    и наоборот.

    Ключ в data задаётся в конструкторе ('owner_bot' или 'driver_bot').
    """

    def __init__(self, other_bot: Bot, key: str):
        super().__init__()
        self.other_bot = other_bot
        self.key = key

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data[self.key] = self.other_bot
        return await handler(event, data)
