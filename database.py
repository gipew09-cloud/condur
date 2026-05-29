"""
Подключение к базе данных.

engine — это "двигатель", который держит соединения с PostgreSQL.
async_session — фабрика сессий. Сессия = одна "рабочая область" для запросов;
открыли, поработали, закрыли. В каждом обработчике бота будет своя сессия.
"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False)
# echo=True покажет в консоли все SQL-запросы — удобно для отладки, потом выключим.

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
