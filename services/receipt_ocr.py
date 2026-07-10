"""
Распознавание суммы (и литров) с фото чека — Блок C.

СЕЙЧАС ДОРМАНТ. Без API-ключа функция выключена и возвращает None, поэтому
бот спрашивает сумму у водителя вручную (как раньше) — это и есть мягкий
фоллбэк. Когда появится ключ: выставьте в переменных окружения
RECEIPT_OCR_PROVIDER (anthropic|openai) и соответствующий *_API_KEY, и оставьте
FEATURE_RECEIPT_OCR=on — тогда сумма будет читаться с фото.

Намеренно без тяжёлых SDK: реальный вызов провайдера пойдёт через httpx
(ленивый импорт внутри функции), чтобы не тянуть зависимость, пока OCR не нужен.
"""
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiptReading:
    """Что удалось прочитать с чека. Любое поле может быть None."""
    amount_rub: Decimal | None
    liters: Decimal | None = None
    raw: str | None = None


def is_enabled() -> bool:
    """OCR активен только если включён флаг И задан ключ нужного провайдера."""
    if not settings.feature_receipt_ocr:
        return False
    provider = (settings.receipt_ocr_provider or "").lower()
    if provider == "anthropic":
        return bool(settings.anthropic_api_key)
    if provider == "openai":
        return bool(settings.openai_api_key)
    return False


async def recognize(image_bytes: bytes) -> ReceiptReading | None:
    """
    Вернуть сумму/литры с фото чека, либо None (OCR выключен / нет ключа / ошибка).
    Никогда не бросает наружу — расход важнее распознавания.
    """
    if not is_enabled():
        return None
    provider = (settings.receipt_ocr_provider or "").lower()
    try:
        if provider == "anthropic":
            return await _recognize_anthropic(image_bytes)
        if provider == "openai":
            return await _recognize_openai(image_bytes)
    except Exception as exc:  # noqa: BLE001 — OCR не критичен, не валим расход
        logger.warning("Receipt OCR failed (%s): %s", provider, exc)
    return None


# Промпт для vision-модели — общий для провайдеров.
_PROMPT = (
    "На фото кассовый чек. Верни ТОЛЬКО JSON без пояснений: "
    '{"amount_rub": <итоговая сумма числом или null>, '
    '"liters": <литры топлива числом или null>}.'
)


async def _recognize_anthropic(image_bytes: bytes) -> ReceiptReading | None:
    # TODO(ключ): POST https://api.anthropic.com/v1/messages через httpx,
    # модель claude-haiku-4-5, content = [image(base64), text(_PROMPT)].
    # Распарсить JSON ответа в ReceiptReading. Заголовки: x-api-key, anthropic-version.
    logger.info("Anthropic receipt OCR not wired yet — dormant")
    return None


async def _recognize_openai(image_bytes: bytes) -> ReceiptReading | None:
    # TODO(ключ): POST https://api.openai.com/v1/chat/completions через httpx,
    # модель gpt-4o-mini, image_url=data:base64. Распарсить JSON в ReceiptReading.
    logger.info("OpenAI receipt OCR not wired yet — dormant")
    return None


def parse_amount(text: str | None) -> Decimal | None:
    """Утилита: строка → Decimal, либо None."""
    if not text:
        return None
    try:
        return Decimal(text.strip().replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None
