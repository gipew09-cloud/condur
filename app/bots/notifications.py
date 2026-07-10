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
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.models import Admin, Owner

logger = logging.getLogger(__name__)


# Что считаем временной проблемой и ретраим (и при скачивании фото, и при
# отправке сообщений): сетевые сбои, 5xx Telegram, rate limit.
# TelegramBadRequest/ForbiddenError НЕ ретраим — это клиентские ошибки,
# повтор бесполезен.
_RETRYABLE_TELEGRAM_ERRORS = (
    TelegramNetworkError,
    TelegramServerError,
    TelegramRetryAfter,
    OSError,  # сетевой уровень: ConnectionError, TimeoutError и т.п.
)


async def _download_with_retry(bot: Bot, file_id: str):
    """3 попытки с экспоненциальной задержкой (1с → 2с → 4с)."""
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE_TELEGRAM_ERRORS),
        reraise=True,
    ):
        with attempt:
            return await bot.download(file_id)


async def _send_message_with_retry(
    bot: Bot, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None
):
    """Отправка сообщения с 3 попытками на временные сбои (сеть, 5xx, rate limit)."""
    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(_RETRYABLE_TELEGRAM_ERRORS),
        reraise=True,
    ):
        with attempt:
            return await bot.send_message(chat_id, text, reply_markup=reply_markup)


async def _disable(session: AsyncSession, owner_id: int) -> None:
    await session.execute(
        update(Owner).where(Owner.id == owner_id).values(notifications_enabled=False)
    )
    await session.commit()


async def _admin_chat_ids(session: AsyncSession, owner_id: int) -> list[int]:
    """Telegram ID админов кабинета, которым дублируем уведомления владельца.

    Второй телефон владельца — это отдельный аккаунт, добавленный админом.
    """
    result = await session.execute(
        select(Admin.telegram_id).where(
            Admin.owner_id == owner_id,
            Admin.notifications_enabled.is_(True),
        )
    )
    return [row[0] for row in result.all()]


async def _copy_to_admins(
    bot: Bot,
    session: AsyncSession,
    owner: Owner,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    """Дубли уведомления админам. Любая ошибка по одному адресату — молча
    пропускаем (админ мог не нажать /start у бота), рассылку не роняем."""
    for chat_id in await _admin_chat_ids(session, owner.id):
        if chat_id == owner.telegram_id:
            continue  # владелец сам добавлен админом — не дублируем ему же
        try:
            await _send_message_with_retry(bot, chat_id, text, reply_markup)
        except Exception as exc:
            logger.info("Admin %s notification skipped: %s", chat_id, exc)


async def notify_owner(
    bot: Bot,
    session: AsyncSession,
    owner: Owner,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int | None:
    """Отправить владельцу сообщение (+дубли всем админам кабинета).
    Возвращает message_id сообщения владельцу или None."""
    if not owner.notifications_enabled or owner.telegram_id is None:
        return None
    message_id: int | None = None
    try:
        sent = await _send_message_with_retry(
            bot, owner.telegram_id, text, reply_markup
        )
        message_id = sent.message_id
    except TelegramForbiddenError:
        logger.warning("Owner %s blocked the bot, disabling notifications", owner.id)
        await _disable(session, owner.id)
    except TelegramBadRequest as exc:
        logger.error("Failed to notify owner %s: %s", owner.id, exc)
    except (RetryError, *_RETRYABLE_TELEGRAM_ERRORS) as exc:
        # временный сбой не разрешился за 3 попытки — логируем и НЕ валим
        # вызывающий код (важно для циклов рассылки по всем владельцам)
        logger.error("Failed to notify owner %s after retries: %s", owner.id, exc)
    await _copy_to_admins(bot, session, owner, text, reply_markup)
    return message_id


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
        buf = await _download_with_retry(source_bot, source_file_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        # клиентская ошибка — file_id битый или бот заблокирован, ретраить нечего
        logger.error("Failed to download photo %s: %s", source_file_id, exc)
        return None
    except (RetryError, *_RETRYABLE_TELEGRAM_ERRORS) as exc:
        # сетевая проблема всё-таки не разрешилась за 3 попытки
        logger.error(
            "Photo %s download failed after retries: %s — file_id остаётся в БД",
            source_file_id, exc,
        )
        return None
    if buf is None:
        return None

    message_id: int | None = None
    try:
        photo_bytes = buf.read()
        photo = BufferedInputFile(photo_bytes, filename="photo.jpg")
        sent = await owner_bot.send_photo(
            owner.telegram_id, photo, caption=caption, reply_markup=reply_markup
        )
        message_id = sent.message_id
    except TelegramForbiddenError:
        logger.warning("Owner %s blocked the bot, disabling notifications", owner.id)
        await _disable(session, owner.id)
    except TelegramBadRequest as exc:
        logger.error("Failed to send photo to owner %s: %s", owner.id, exc)
    finally:
        buf.close()

    # дубли фото админам (второй телефон владельца); ошибки — молча пропускаем
    for chat_id in await _admin_chat_ids(session, owner.id):
        if chat_id == owner.telegram_id:
            continue
        try:
            await owner_bot.send_photo(
                chat_id,
                BufferedInputFile(photo_bytes, filename="photo.jpg"),
                caption=caption,
                reply_markup=reply_markup,
            )
        except Exception as exc:
            logger.info("Admin %s photo notification skipped: %s", chat_id, exc)
    return message_id


async def drop_revenue_prompt_buttons(
    session: AsyncSession, trip_id: int, *, owner_bot: Bot | None = None,
    driver_bot: Bot | None = None, side: str,
    event_type: str = "trip_revenue_prompt",
) -> None:
    """Погасить устаревшую кнопку «Указать выручку» у второй стороны.

    Когда сумму первым указал водитель — у ВЛАДЕЛЬЦА кнопка «Указать выручку»
    больше не нужна (ему придёт «Одобрить/Изменить»). И наоборот: указал
    владелец — у водителя кнопка гаснет. Дешёво: один edit-запрос в Telegram.
    side: 'owner' — гасим у владельца, 'driver' — у водителя.
    """
    from sqlalchemy import desc as _desc

    from app.models import Event

    row = (
        await session.execute(
            select(Event.payload)
            .where(Event.trip_id == trip_id, Event.event_type == event_type)
            .order_by(_desc(Event.created_at), _desc(Event.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if not row:
        return
    if side == "owner":
        bot, chat_id, msg_id = owner_bot, row.get("owner_chat_id"), row.get("owner_msg_id")
    else:
        bot, chat_id, msg_id = driver_bot, row.get("driver_chat_id"), row.get("driver_msg_id")
    if bot is None or not chat_id or not msg_id:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=msg_id, reply_markup=None
        )
    except Exception as exc:  # noqa: BLE001 — сообщение могли удалить/устареть
        logger.info("Не смог убрать кнопку выручки (%s, trip=%s): %s", side, trip_id, exc)


async def drop_revenue_decision_buttons(
    session: AsyncSession, trip_id: int, *, owner_bot: Bot
) -> None:
    """Погасить «Одобрить/Изменить» у владельца, когда выручка уже закрыта
    (одобрена или вписана вручную) — чтобы не осталось второй живой кнопки."""
    await drop_revenue_prompt_buttons(
        session, trip_id, owner_bot=owner_bot, side="owner",
        event_type="trip_revenue_decision_prompt",
    )


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
