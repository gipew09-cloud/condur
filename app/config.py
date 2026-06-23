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
    redis_url: str = ""  # необязателен пока используем MemoryStorage

    # секрет для подписи JWT в веб-кабинете. На Railway задайте через Variables.
    jwt_secret: str = "change-me-in-production"
    # HTTP-порт для FastAPI. Railway пробрасывает свой через переменную PORT.
    port: int = 8000
    # дефолтная таймзона: подставляется новым владельцам при регистрации
    default_timezone: str = "Europe/Moscow"
    # Карта водителей — OpenStreetMap + Leaflet + CartoDB-плитки.
    # Без API-ключа, бесплатно. Если когда-нибудь захотим Яндекс или 2GIS —
    # они оба требуют регистрацию ключа, делать через ENV-переменную.

    # =====================================================================
    # ФИЧА-ФЛАГИ бота водителя (Блок A).
    # Принцип: выключено = водитель функцию НЕ видит (кнопка/шаг скрыты);
    # включишь позже = появится, без нового бота и без дублирования кода.
    # Код спрятанных функций остаётся рабочим — просто за выключенным флагом.
    # ENV принимает on/off, true/false, 1/0 (pydantic парсит регистронезависимо).
    # =====================================================================
    feature_trip_cargo: bool = False            # вопрос «что везёте»
    feature_odometer_photo: bool = False        # фото одометра + ввод значения
    feature_trip_status_steps: bool = False     # промежуточные статусы рейса (выгрузка)
    feature_cargo_geolocation: bool = True      # геопозиция при сдаче груза (включена — наполняет карту)
    feature_cash_handover: bool = False         # «сдал деньги»
    feature_show_salary: bool = False           # показ зарплаты в конце смены
    feature_notify_driver_approval: bool = False  # уведомлять водителя об одобрении расхода
    feature_receipt_ocr: bool = True            # распознавание суммы с чека (дормант без ключа)
    feature_odometer_ocr: bool = False          # распознавание одометра с фото (на будущее)
    feature_downtime: bool = False              # кнопка «Простой» (нет в списке «оставить»)

    # OCR чека (Блок C). Без ключа функция дормант — водитель вводит сумму руками.
    # provider: anthropic | openai | disabled. Ключи — через ENV, в код не пишем.
    receipt_ocr_provider: str = "anthropic"
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("redis_url")
    @classmethod
    def fix_redis_url(cls, v: str) -> str:
        # Пустая строка допустима: значит используем MemoryStorage
        if not v:
            return v
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
