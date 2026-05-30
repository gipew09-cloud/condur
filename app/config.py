"""
Настройки проекта. pydantic-settings читает переменные:
  - локально — из .env;
  - на Railway — напрямую из переменных окружения (для них .env не нужен).

Особенности Railway:
  1) Плагин Postgres отдаёт DATABASE_URL обычно как postgresql://...
     (изредка postgres:// — наследие Heroku). SQLAlchemy + asyncpg хотят
     postgresql+asyncpg://... — чиним валидатором.
  2) Плагин Redis иногда отдаёт REDIS_URL без схемы — просто host:port.
     aiogram RedisStorage требует redis:// — тоже чиним валидатором.

Оба валидатора не ломают локальный .env: если префикс уже правильный,
возвращаем строку как есть.
"""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    owner_bot_token: str
    driver_bot_token: str
    database_url: str
    redis_url: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("redis_url")
    @classmethod
    def fix_redis_url(cls, v: str) -> str:
        if not v.startswith(("redis://", "rediss://", "unix://")):
            return f"redis://{v}"
        return v

    @field_validator("database_url")
    @classmethod
    def fix_database_url(cls, v: str) -> str:
        # Heroku-style: postgres:// → postgresql+asyncpg://
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        # Railway-style: postgresql:// → postgresql+asyncpg://
        if v.startswith("postgresql://") and not v.startswith("postgresql+"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v


settings = Settings()
