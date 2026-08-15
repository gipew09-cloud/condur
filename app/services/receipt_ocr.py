"""
Распознавание суммы/литров с фото чека и показаний одометра — Блок C.

Поддерживаемые провайдеры (RECEIPT_OCR_PROVIDER в env):
  gemini   — Google Gemini 1.5 Flash (1500 запросов/день БЕСПЛАТНО)
             Ключ: GEMINI_API_KEY из https://aistudio.google.com/app/apikey
  anthropic — Claude Haiku (платно, ~$0.001/фото)
             Ключ: ANTHROPIC_API_KEY
  openai   — GPT-4o-mini (платно, ~$0.002/фото)
             Ключ: OPENAI_API_KEY

Фоллбэк: без ключа / при ошибке бот спрашивает сумму у водителя вручную.
Намеренно без тяжёлых SDK — только httpx (уже есть в зависимостях aiogram).
"""
import base64
import json
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiptReading:
    """Что удалось прочитать. Любое поле может быть None."""
    amount_rub: Decimal | None
    liters: Decimal | None = None
    raw: str | None = None


@dataclass(frozen=True)
class OdometerReading:
    """Показание одометра с фото."""
    km: int | None
    raw: str | None = None


def is_enabled() -> bool:
    """OCR активен только если включён флаг И задан ключ нужного провайдера."""
    if not settings.feature_receipt_ocr:
        return False
    return bool(_get_provider_key())


def _get_provider() -> str:
    return (settings.receipt_ocr_provider or "").lower()


def _get_provider_key() -> str:
    provider = _get_provider()
    if provider == "gemini":
        return getattr(settings, "gemini_api_key", "") or ""
    if provider == "anthropic":
        return settings.anthropic_api_key or ""
    if provider == "openai":
        return settings.openai_api_key or ""
    return ""


# -------------------------------------------------------------------------
# Промпты
# -------------------------------------------------------------------------
_RECEIPT_PROMPT = (
    "На фото кассовый чек АЗС или магазина. "
    "Найди ИТОГОВУЮ сумму к оплате и количество литров топлива (если есть). "
    "Верни ТОЛЬКО валидный JSON без пояснений и markdown: "
    '{"amount_rub": <число или null>, "liters": <число или null>}. '
    "Пример: {\"amount_rub\": 4250.50, \"liters\": 42.5}"
)

_ODOMETER_PROMPT = (
    "На фото приборная панель автомобиля. "
    "Найди показание одометра — общий пробег в километрах. "
    "Верни ТОЛЬКО валидный JSON без пояснений и markdown: "
    '{"km": <целое число или null>}. '
    "Пример: {\"km\": 154820}"
)


# -------------------------------------------------------------------------
# Публичный API
# -------------------------------------------------------------------------
async def recognize(image_bytes: bytes) -> ReceiptReading | None:
    """
    Прочитать сумму/литры с фото чека.
    Возвращает None если OCR выключен, нет ключа или произошла ошибка.
    Никогда не бросает наружу.
    """
    if not is_enabled():
        return None
    provider = _get_provider()
    try:
        raw = await _call_vision(image_bytes, _RECEIPT_PROMPT)
        if raw is None:
            return None
        return _parse_receipt(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Receipt OCR failed (%s): %s", provider, exc)
    return None


async def recognize_odometer(image_bytes: bytes) -> OdometerReading | None:
    """
    Прочитать показание одометра с фото.
    Возвращает None если OCR выключен или ошибка.
    """
    if not is_enabled():
        return None
    provider = _get_provider()
    try:
        raw = await _call_vision(image_bytes, _ODOMETER_PROMPT)
        if raw is None:
            return None
        return _parse_odometer(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Odometer OCR failed (%s): %s", provider, exc)
    return None


# -------------------------------------------------------------------------
# Роутинг по провайдеру
# -------------------------------------------------------------------------
async def _call_vision(image_bytes: bytes, prompt: str) -> str | None:
    """Вызвать выбранный провайдер. Возвращает сырой текст ответа или None."""
    provider = _get_provider()
    if provider == "gemini":
        return await _gemini(image_bytes, prompt)
    if provider == "anthropic":
        return await _anthropic(image_bytes, prompt)
    if provider == "openai":
        return await _openai(image_bytes, prompt)
    logger.warning("Неизвестный OCR провайдер: %s", provider)
    return None


# -------------------------------------------------------------------------
# Gemini 1.5 Flash — 1500 бесплатных запросов в день
# Ключ: GEMINI_API_KEY из https://aistudio.google.com/app/apikey
# -------------------------------------------------------------------------
async def _gemini(image_bytes: bytes, prompt: str) -> str | None:
    import httpx

    api_key = _get_provider_key()
    if not api_key:
        return None

    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "contents": [
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                    {"text": prompt},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0,          # детерминированный ответ
            "maxOutputTokens": 64,     # нам нужен только короткий JSON
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={api_key}"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
    data = resp.json()
    # Путь к тексту ответа в Gemini API
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        logger.warning("Неожиданный формат ответа Gemini: %s", data)
        return None


# -------------------------------------------------------------------------
# Anthropic Claude Haiku — платно, ~$0.001/фото
# -------------------------------------------------------------------------
async def _anthropic(image_bytes: bytes, prompt: str) -> str | None:
    import httpx

    api_key = _get_provider_key()
    if not api_key:
        return None

    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": "claude-haiku-4-5",
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            json=payload,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        resp.raise_for_status()
    data = resp.json()
    try:
        return data["content"][0]["text"]
    except (KeyError, IndexError):
        return None


# -------------------------------------------------------------------------
# OpenAI GPT-4o-mini — платно, ~$0.002/фото
# -------------------------------------------------------------------------
async def _openai(image_bytes: bytes, prompt: str) -> str | None:
    import httpx

    api_key = _get_provider_key()
    if not api_key:
        return None

    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": "gpt-4o-mini",
        "max_tokens": 64,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return None


# -------------------------------------------------------------------------
# Парсинг JSON из ответа модели
# -------------------------------------------------------------------------
def _extract_json(text: str) -> dict:
    """
    Извлечь первый JSON-объект из текста.
    Модели иногда оборачивают ответ в ```json ... ```.
    """
    text = text.strip()
    # убираем markdown-блоки кода если есть
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(
            line for line in lines
            if not line.strip().startswith("```")
        )
    # ищем первый { ... }
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"Нет JSON в ответе модели: {text!r}")
    return json.loads(text[start:end])


def _parse_receipt(raw: str) -> ReceiptReading | None:
    try:
        data = _extract_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Не удалось распарсить JSON чека: %s | raw=%r", exc, raw[:200])
        return None

    amount = _to_decimal(data.get("amount_rub"))
    liters = _to_decimal(data.get("liters"))

    # Санитарная проверка: сумма не может быть 0 или миллион
    if amount is not None and not (Decimal("1") <= amount <= Decimal("999999")):
        logger.info("OCR вернул неправдоподобную сумму %s, игнорируем", amount)
        amount = None

    if amount is None and liters is None:
        return None
    return ReceiptReading(amount_rub=amount, liters=liters, raw=raw[:500])


def _parse_odometer(raw: str) -> OdometerReading | None:
    try:
        data = _extract_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Не удалось распарсить JSON одометра: %s | raw=%r", exc, raw[:200])
        return None

    km_raw = data.get("km")
    if km_raw is None:
        return None
    try:
        km = int(km_raw)
    except (TypeError, ValueError):
        return None

    # Одометр: от 0 до 9 999 999 км
    if not (0 <= km <= 9_999_999):
        logger.info("OCR вернул неправдоподобный одометр %s, игнорируем", km)
        return None
    return OdometerReading(km=km, raw=raw[:200])


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None


def parse_amount(text: str | None) -> Decimal | None:
    """Утилита: строка → Decimal, либо None."""
    if not text:
        return None
    try:
        return Decimal(text.strip().replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None
