"""
Отправка уведомлений владельцу из бота водителя (и наоборот).

Важно: если адресат заблокировал бота или telegram_id не найден,
TelegramBadRequest/TelegramForbiddenError не должны валить процесс.
Мы логируем и помечаем в БД notifications_enabled=False, чтобы
больше не спамить.

Тонкость с фото: file_id привязан к конкретному боту. driver_bot
получил фото — у него file_id "A...", но owner_bot этот file_id не
знает. Поэтому для пересылки качаем bytes через бот-источник и
загружаем как BufferedInputFile через бот-получатель.
"""
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Owner

logger = logging.getLogger(__name__)


async def _disable(session: AsyncSession, owner_id: int) -> None:
    await session.execute(
        update(Owner).where(Owner.id == owner_id).values(notifications_enabled=False)
    )
    await session.commit()


async def notify_owner(
    bot: Bot,
    session: AsyncSession,
    owner: Owner,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int | None:
    """Отправить владельцу сообщение. Возвращает message_id или None."""
    if not owner.notifications_enabled or owner.telegram_id is None:
        return None
    try:
        sent = await bot.send_message(owner.telegram_id, text, reply_markup=reply_markup)
        return sent.message_id
    except TelegramForbiddenError:
        logger.warning("Owner %s blocked the bot, disabling notifications", owner.id)
        await _disable(session, owner.id)
        return None
    except TelegramBadRequest as exc:
        logger.error("Failed to notify owner %s: %s", owner.id, exc)
        return None


async def transfer_photo_to_owner(
    *,
    source_bot: Bot,
    owner_bot: Bot,
    session: AsyncSession,
    owner: Owner,
    source_file_id: str,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int | None:
    """
    Скачать фото через source_bot и отправить владельцу через owner_bot.
    Возвращает message_id или None.
    """
    if not owner.notifications_enabled or owner.telegram_id is None:
        return None
    try:
        buf = await source_bot.download(source_file_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.error("Failed to download photo %s: %s", source_file_id, exc)
        return None
    if buf is None:
        return None

    try:
        photo = BufferedInputFile(buf.read(), filename="photo.jpg")
        sent = await owner_bot.send_photo(
            owner.telegram_id, photo, caption=caption, reply_markup=reply_markup
        )
        return sent.message_id
    except TelegramForbiddenError:
        logger.warning("Owner %s blocked the bot, disabling notifications", owner.id)
        await _disable(session, owner.id)
        return None
    except TelegramBadRequest as exc:
        logger.error("Failed to send photo to owner %s: %s", owner.id, exc)
        return None
    finally:
        buf.close()


async def notify_driver(
    bot: Bot,
    session: AsyncSession,
    driver_telegram_id: int | None,
    text: str,
) -> bool:
    """Сообщение водителю. На ошибке просто логируем."""
    if driver_telegram_id is None:
        return False
    try:
        await bot.send_message(driver_telegram_id, text)
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logger.warning("Failed to notify driver %s: %s", driver_telegram_id, exc)
        return False
