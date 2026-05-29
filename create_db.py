"""
Создаёт все таблицы в базе на основе моделей.
Запускается один раз (или после изменения моделей на этапе разработки):
    python create_db.py

Позже, когда схема устоится, перейдём на Alembic (миграции) — он умеет
менять таблицы без потери данных. Сейчас для простоты — create_all.
"""
import asyncio

from app.database import engine
from app.models import Base


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Таблицы созданы.")


if __name__ == "__main__":
    asyncio.run(main())
