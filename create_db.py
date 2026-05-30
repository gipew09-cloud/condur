"""
Создание / миграция схемы.

Запускается в Railway при каждом старте контейнера (см. startCommand в
railway.json: `python create_db.py && python -m app.main`).

Делает две вещи:
  1) Base.metadata.create_all — создаёт таблицы, которых ещё нет
     (идемпотентно: существующие не трогает).
  2) Применяет вручную дописанные ALTER'ы — для случаев, когда мы
     добавляли колонки в существующую таблицу после первого деплоя.
     create_all так не умеет, поэтому пишем явные ADD COLUMN IF NOT EXISTS.

Логика «если новой колонки нет — добавь, если CheckConstraint устарел —
пересоздай» позволяет не вспоминать про миграцию вручную в дашборде
Railway. Альтернатива — Alembic, но для текущего размера команды overkill.
"""
import asyncio
import logging

from sqlalchemy import text

from app.database import engine
from app.models import Base

logger = logging.getLogger(__name__)

# Каждая строка — отдельный SQL, выполняемый по одной.
# Все формулировки идемпотентные: повторный запуск ничего не ломает.
MIGRATIONS = [
    # === Этап 2Б: новый тип ЗП per_trip ===
    "ALTER TABLE drivers DROP CONSTRAINT IF EXISTS ck_driver_salary_type",
    "ALTER TABLE drivers ADD CONSTRAINT ck_driver_salary_type "
    "CHECK (salary_type IN ('per_km','per_trip','percent','fixed_per_shift'))",

    # === Время начала смены у водителя (для late-start алертов) ===
    "ALTER TABLE drivers ADD COLUMN IF NOT EXISTS shift_start_time VARCHAR(5)",

    # === Доки на машину (для алертов истечения) ===
    "ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS osago_expires DATE",
    "ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS inspection_expires DATE",
    "ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS tacho_expires DATE",
]


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("✅ Таблицы созданы / обновлены через create_all.")

    # Отдельные миграции — каждая своей транзакцией, чтобы одна неудача
    # не блокировала остальные.
    for sql in MIGRATIONS:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
            print(f"  · OK: {sql[:80]}{'…' if len(sql) > 80 else ''}")
        except Exception as exc:  # noqa: BLE001
            print(f"  · SKIP: {sql[:80]}… ({exc.__class__.__name__})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
