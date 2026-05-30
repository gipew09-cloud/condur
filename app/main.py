"""
Точка входа. Запускает в одном процессе:
  - бот владельца (long-polling),
  - бот водителя (long-polling),
  - FastAPI веб-кабинет на uvicorn.

Всё через единый asyncio.gather. Если один из трёх упадёт — упадёт весь
процесс, Railway пере запустит (restartPolicyType=ON_FAILURE).

Состояние FSM держим в памяти (MemoryStorage). При рестарте незавершённые
диалоги обнуляются — на MVP это приемлемо.

Кросс-бот middleware прокидывает в хендлеры "соседнего" бота, чтобы
driver_bot мог отправить уведомление через owner_bot, и наоборот.
"""
import asyncio
import logging
import os

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bots.driver_bot import driver_router
from app.bots.middlewares import CrossBotMiddleware, DbSessionMiddleware
from app.bots.owner_bot import owner_router
from app.config import settings
from app.database import async_session
from app.services.scheduler_jobs import (
    daily_summary_job,
    doc_expiry_job,
    late_start_job,
    silence_detector_job,
)
from app.web.router import app as web_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _build_storage() -> BaseStorage:
    """
    RedisStorage если задан REDIS_URL, иначе MemoryStorage с предупреждением.
    Redis нужен чтобы FSM-диалоги (онбординг, добавление машины и т.п.)
    переживали рестарт контейнера. На MemoryStorage всё, что было в процессе
    диалога на момент рестарта, теряется — пользователь оказывается «в начале».
    """
    if settings.redis_url:
        logging.info("FSM storage: Redis (%s)", _redact_url(settings.redis_url))
        return RedisStorage.from_url(settings.redis_url)
    logging.warning(
        "REDIS_URL пустой — используем MemoryStorage. "
        "Незавершённые диалоги будут теряться при рестарте."
    )
    return MemoryStorage()


def _redact_url(url: str) -> str:
    """Маскируем пароль в URL для логов: redis://default:SECRET@host -> redis://default:***@host"""
    import re as _re
    return _re.sub(r"(://[^:@/]+:)[^@/]+(@)", r"\1***\2", url)


async def main() -> None:
    storage = _build_storage()
    default = DefaultBotProperties(parse_mode=ParseMode.HTML)

    owner_bot = Bot(token=settings.owner_bot_token, default=default)
    driver_bot = Bot(token=settings.driver_bot_token, default=default)

    owner_dp = Dispatcher(storage=storage)
    driver_dp = Dispatcher(storage=storage)

    db_mw = DbSessionMiddleware(async_session)
    for dp in (owner_dp, driver_dp):
        dp.message.middleware(db_mw)
        dp.callback_query.middleware(db_mw)

    owner_side_cross = CrossBotMiddleware(driver_bot, key="driver_bot")
    owner_dp.message.middleware(owner_side_cross)
    owner_dp.callback_query.middleware(owner_side_cross)

    driver_side_cross = CrossBotMiddleware(owner_bot, key="owner_bot")
    driver_dp.message.middleware(driver_side_cross)
    driver_dp.callback_query.middleware(driver_side_cross)

    owner_dp.include_router(owner_router)
    driver_dp.include_router(driver_router)

    # На Railway порт приходит в PORT, локально берём из настроек
    port = int(os.environ.get("PORT") or settings.port)
    uv_config = uvicorn.Config(
        web_app, host="0.0.0.0", port=port, log_level="info", access_log=False
    )
    uv_server = uvicorn.Server(uv_config)

    # Планировщик: дневная сводка, документы, late-start.
    # Все три задачи проверяют локальное время каждого владельца внутри себя,
    # поэтому крутим их с UTC-cron-триггера.
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(daily_summary_job, "cron", minute="*/30", args=[owner_bot])
    scheduler.add_job(doc_expiry_job, "cron", minute="*/30", args=[owner_bot])
    scheduler.add_job(late_start_job, "cron", minute="*/15", args=[owner_bot])
    scheduler.add_job(
        silence_detector_job, "cron", minute="*/30", args=[owner_bot],
        max_instances=1, misfire_grace_time=60,
    )
    scheduler.start()

    logging.info("Боты, веб-кабинет и планировщик запущены. Порт: %s. Ctrl+C для остановки.", port)
    try:
        await asyncio.gather(
            owner_dp.start_polling(owner_bot),
            driver_dp.start_polling(driver_bot),
            uv_server.serve(),
        )
    finally:
        scheduler.shutdown(wait=False)
        await owner_bot.session.close()
        await driver_bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
