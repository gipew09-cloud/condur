"""
Отправка уведомлений владельцу из бота водителя (и наоборот).

Важно: если владелец заблокировал бота, или telegram_id не найден,
TelegramBadRequest/TelegramForbiddenError не должны валить процесс.
Мы логируем и помечаем в БД notifications_enabled=False, чтобы
больше не спамить.
"""
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Owner

logger = logging.getLogger(__name__)


async def notify_owner(
    bot: Bot,
    session: AsyncSession,
    owner: Owner,
    text: str,
    reply_markup=None,
) -> bool:
    """Отправить владельцу сообщение. Возвращает True, если доставлено."""
    if not owner.notifications_enabled or owner.telegram_id is None:
        return False
    try:
        await bot.send_message(owner.telegram_id, text, reply_markup=reply_markup)
        return True
    except TelegramForbiddenError:
        logger.warning("Owner %s blocked the bot, disabling notifications", owner.id)
        await session.execute(
            update(Owner).where(Owner.id == owner.id).values(notifications_enabled=False)
        )
        await session.commit()
        return False
    except TelegramBadRequest as exc:
        logger.error("Failed to notify owner %s: %s", owner.id, exc)
        return False


async def notify_owner_with_photo(
    bot: Bot,
    session: AsyncSession,
    owner: Owner,
    photo_file_id: str,
    caption: str,
) -> bool:
    """Отправить владельцу фото с подписью."""
    if not owner.notifications_enabled or owner.telegram_id is None:
        return False
    try:
        await bot.send_photo(owner.telegram_id, photo_file_id, caption=caption)
        return True
    except TelegramForbiddenError:
        logger.warning("Owner %s blocked the bot, disabling notifications", owner.id)
        await session.execute(
            update(Owner).where(Owner.id == owner.id).values(notifications_enabled=False)
        )
        await session.commit()
        return False
    except TelegramBadRequest as exc:
        logger.error("Failed to notify owner %s with photo: %s", owner.id, exc)
        return False
