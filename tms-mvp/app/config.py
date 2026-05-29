"""
Настройки проекта. pydantic-settings сам читает переменные из файла .env
и проверяет, что они есть. Если какой-то токен забыли — программа сразу
скажет об этом при запуске, а не упадёт позже в непонятном месте.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    owner_bot_token: str
    driver_bot_token: str
    database_url: str
    redis_url: str

    # говорим, откуда читать переменные
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


# один общий объект настроек на весь проект — импортируем его везде
settings = Settings()
