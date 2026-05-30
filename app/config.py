"""
Настройки проекта. pydantic-settings сам читает переменные из файла .env
(локально) или из окружения (на Railway) и проверяет, что они есть.

Особенности Railway:
  1) Плагин Postgres отдаёт DATABASE_URL в формате
        postgresql://user:pass@host:port/db
     (или иногда postgres:// — наследие Heroku).
     SQLAlchemy + asyncpg требуют префикс
        postgresql+asyncpg://...

  2) Плагин Redis иногда отдаёт REDIS_URL без схемы — просто
        default:password@host:port
     aiogram RedisStorage требует префикс redis:// (или rediss://, unix://).

  Оба случая чиним field-валидаторами ниже, не ломая локальный .env.
"""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    owner_bot_token: str
    driver_bot_token: str
    database_url: str
    redis_url: str

    @field_validator("database_url")
    @classmethod
    def _ensure_asyncpg(cls, v: str) -> str:
        # Railway / Heroku-style URL → SQLAlchemy async URL
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v

    @field_validator("redis_url")
    @classmethod
    def _ensure_redis_scheme(cls, v: str) -> str:
        # Railway иногда отдаёт REDIS_URL без префикса схемы — допишем сами
        if not v.startswith(("redis://", "rediss://", "unix://")):
            return f"redis://{v}"
        return v

    # Локально читаем из .env, на Railway переменные приходят из окружения
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


# один общий объект настроек на весь проект — импортируем его везде
settings = Settings()
