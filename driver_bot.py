"""
Бот ВОДИТЕЛЯ.

Что умеет на Этапе 1:
  - /start <invite_token>: привязать telegram_id к существующему Driver
  - /start без токена: распознать водителя по telegram_id и поздороваться
  - /start без токена и без регистрации: попросить ссылку от владельца

Меню «начать смену / новый рейс» появится на Этапе 2.
"""
import logging

from aiogram import Bot, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import messages as msg
from app.bots.notifications import notify_owner
from app.models import Driver, Owner

logger = logging.getLogger(__name__)
driver_router = Router()


async def _driver_by_telegram(session: AsyncSession, telegram_id: int) -> Driver | None:
    result = await session.execute(select(Driver).where(Driver.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def _driver_by_invite(session: AsyncSession, token: str) -> Driver | None:
    result = await session.execute(select(Driver).where(Driver.invite_token == token))
    return result.scalar_one_or_none()


@driver_router.message(CommandStart(deep_link=True))
async def start_with_token(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    owner_bot: Bot,
) -> None:
    """/start <token> — привязка водителя по приглашению."""
    await state.clear()
    token = (command.args or "").strip()
    if not token:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return

    # Уже зарегистрирован? Покажем приветствие, токен не трогаем.
    existing = await _driver_by_telegram(session, message.from_user.id)
    if existing is not None:
        await message.answer(msg.DRIVER_WELCOME_BACK.format(name=existing.full_name))
        return

    driver = await _driver_by_invite(session, token)
    if driver is None or driver.telegram_id is not None:
        await message.answer(msg.DRIVER_INVITE_INVALID)
        return

    driver.telegram_id = message.from_user.id
    driver.invite_token = None
    await session.commit()

    await message.answer(msg.DRIVER_REGISTERED.format(name=driver.full_name))

    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot,
            session,
            owner,
            msg.NOTIFY_DRIVER_REGISTERED.format(name=driver.full_name),
        )


@driver_router.message(CommandStart())
async def start_no_token(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """/start без токена — либо приветствие, либо «попросите ссылку»."""
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    await message.answer(msg.DRIVER_WELCOME_BACK.format(name=driver.full_name))


@driver_router.message()
async def fallback(message: Message, session: AsyncSession) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    await message.answer(msg.UNKNOWN_COMMAND)
