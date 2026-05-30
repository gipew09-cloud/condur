"""
Точка входа. Запускает ОБА бота одновременно в одном процессе.

Как это работает:
  - создаём два объекта Bot (по токену на каждого),
  - на каждого свой Dispatcher (он раздаёт входящие сообщения в обработчики),
  - состояния (FSM) храним в памяти процесса (MemoryStorage). Это значит,
    что при перезапуске процесса все незавершённые диалоги обнуляются.
    Для MVP это допустимо; позже вернёмся к RedisStorage для устойчивости,
  - middleware прокидывает в хендлеры сессию БД и инстанс "соседнего" бота
    (чтобы driver_bot мог отправить уведомление через owner_bot, и наоборот),
  - asyncio.gather запускает опрос (polling) обоих ботов параллельно.

Запуск:  python -m app.main
Остановка: Ctrl+C
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.bots.driver_bot import driver_router
from app.bots.middlewares import CrossBotMiddleware, DbSessionMiddleware
from app.bots.owner_bot import owner_router
from app.config import settings
from app.database import async_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main() -> None:
    storage = MemoryStorage()
    default = DefaultBotProperties(parse_mode=ParseMode.HTML)

    owner_bot = Bot(token=settings.owner_bot_token, default=default)
    driver_bot = Bot(token=settings.driver_bot_token, default=default)

    owner_dp = Dispatcher(storage=storage)
    driver_dp = Dispatcher(storage=storage)

    # БД-сессия в каждый хендлер
    db_mw = DbSessionMiddleware(async_session)
    for dp in (owner_dp, driver_dp):
        dp.message.middleware(db_mw)
        dp.callback_query.middleware(db_mw)

    # Прокидываем "соседнего" бота: owner_bot нужен в driver-хендлерах, и наоборот
    owner_side_cross = CrossBotMiddleware(driver_bot, key="driver_bot")
    owner_dp.message.middleware(owner_side_cross)
    owner_dp.callback_query.middleware(owner_side_cross)

    driver_side_cross = CrossBotMiddleware(owner_bot, key="owner_bot")
    driver_dp.message.middleware(driver_side_cross)
    driver_dp.callback_query.middleware(driver_side_cross)

    owner_dp.include_router(owner_router)
    driver_dp.include_router(driver_router)

    logging.info("Боты запущены. Ctrl+C для остановки.")
    try:
        await asyncio.gather(
            owner_dp.start_polling(owner_bot),
            driver_dp.start_polling(driver_bot),
        )
    finally:
        await owner_bot.session.close()
        await driver_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
