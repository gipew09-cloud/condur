"""
Распознавание суммы/литров с фото чека и показаний одометра — Блок C.

Поддерживаемые провайдеры (RECEIPT_OCR_PROVIDER в env):
  llamaparse — LlamaParse, 10 000 страниц в месяц БЕСПЛАТНО, файл до ~20 МБ
             Ключ: LLAMA_CLOUD_API_KEY из cloud.llamaindex.ai → API Keys
             ⚠️ Это НЕ модель со зрением: он отдаёт распознанный ТЕКСТ, а сумму
             и литры из него достаёт наш разбор (parse_receipt_text ниже).
  gemini   — Google Gemini 1.5 Flash (1500 запросов/день БЕСПЛАТНО)
             Ключ: GEMINI_API_KEY из https://aistudio.google.com/app/apikey
  anthropic — Claude Haiku (платно, ~$0.001/фото)
             Ключ: ANTHROPIC_API_KEY
  openai   — GPT-4o-mini (платно, ~$0.002/фото)
             Ключ: OPENAI_API_KEY

Фоллбэк: без ключа / при ошибке бот спрашивает сумму у водителя вручную.
Намеренно без тяжёлых SDK — только httpx (уже есть в зависимостях aiogram).
"""
import asyncio
import base64
import json
import logging
import re
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


# Провайдеры, которые возвращают просто текст со снимка, а не готовый JSON.
# Для них сумму и литры вытаскивает наш разбор, а не модель.
_TEXT_PROVIDERS = {"llamaparse"}


def _get_provider_key() -> str:
    provider = _get_provider()
    if provider == "llamaparse":
        return getattr(settings, "llama_cloud_api_key", "") or ""
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
        if provider in _TEXT_PROVIDERS:
            text = await _call_text_ocr(image_bytes)
            return parse_receipt_text(text) if text else None
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
        if provider in _TEXT_PROVIDERS:
            text = await _call_text_ocr(image_bytes)
            return parse_odometer_text(text) if text else None
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
# LlamaParse — 10 000 страниц в месяц бесплатно, лимита в 1 МБ нет
# Ключ: LLAMA_CLOUD_API_KEY, кабинет cloud.llamaindex.ai
#
# Работает не как «модель со зрением»: файл ставится в очередь, задание
# выполняется, потом забираем распознанный текст. Поэтому три шага:
# загрузить → дождаться → забрать разметку.
# -------------------------------------------------------------------------
_LLAMA_BASE = "https://api.cloud.llamaindex.ai/api/v2/parse"
_LLAMA_POLL_S = 1.5          # как часто спрашивать «готово?»
_LLAMA_TIMEOUT_S = 20.0      # дольше водитель ждать не будет — уйдём на ручной ввод


async def _call_text_ocr(image_bytes: bytes) -> str | None:
    """Текстовый OCR выбранного провайдера. Никогда не бросает наружу."""
    if _get_provider() == "llamaparse":
        return await _llamaparse(image_bytes)
    logger.warning("Неизвестный текстовый OCR провайдер: %s", _get_provider())
    return None


def _collect_text(node) -> list[str]:
    """Собрать все текстовые куски из ответа, каким бы он ни пришёл.

    Форма ответа у LlamaParse со временем менялась (страницы, markdown, text),
    и завязываться на один конкретный путь — значит однажды молча получить
    пустоту. Обходим дерево и берём всё, что похоже на распознанный текст.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("markdown", "text", "md") and isinstance(value, str) and value.strip():
                found.append(value)
            else:
                found.extend(_collect_text(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_text(item))
    return found


async def _llamaparse(image_bytes: bytes) -> str | None:
    import httpx

    api_key = _get_provider_key()
    if not api_key:
        return None
    headers = {"Authorization": f"Bearer {api_key}", "accept": "application/json"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        upload = await client.post(
            f"{_LLAMA_BASE}/upload",
            headers=headers,
            files={"file": ("receipt.jpg", image_bytes, "image/jpeg")},
        )
        upload.raise_for_status()
        job_id = (upload.json() or {}).get("id")
        if not job_id:
            logger.warning("LlamaParse не вернул id задания: %s", upload.text[:200])
            return None

        # ждём, пока задание отработает
        waited = 0.0
        while waited < _LLAMA_TIMEOUT_S:
            await asyncio.sleep(_LLAMA_POLL_S)
            waited += _LLAMA_POLL_S
            state = await client.get(f"{_LLAMA_BASE}/{job_id}", headers=headers)
            state.raise_for_status()
            body = state.json() or {}
            status = str((body.get("job") or body).get("status") or "").upper()
            if status in ("SUCCESS", "COMPLETED", "PARTIAL_SUCCESS"):
                break
            if status in ("ERROR", "FAILED", "CANCELLED"):
                logger.warning("LlamaParse вернул статус %s", status)
                return None
        else:
            logger.info("LlamaParse не успел за %s с — уходим на ручной ввод", _LLAMA_TIMEOUT_S)
            return None

        result = await client.get(
            f"{_LLAMA_BASE}/{job_id}", headers=headers, params={"expand": "markdown"}
        )
        result.raise_for_status()

    chunks = _collect_text(result.json())
    text = "\n".join(chunks).strip()
    # видно в логах Railway: заработал ключ или нет, и сколько текста пришло
    logger.info("LlamaParse: задание %s, распознано %s символов", job_id, len(text))
    return text or None


# -------------------------------------------------------------------------
# Разбор чека из ТЕКСТА (для провайдеров, которые не умеют отдавать JSON)
# -------------------------------------------------------------------------
# Порядок важен: сначала самые надёжные слова. «Сумма» ниже «Итого», потому
# что в чеке «сумма» встречается и у отдельной позиции, и у НДС.
_TOTAL_WORDS = (
    "ИТОГО К ОПЛАТЕ", "ИТОГ К ОПЛАТЕ", "К ОПЛАТЕ", "ИТОГО", "ИТОГ",
    "ВСЕГО К ОПЛАТЕ", "ВСЕГО", "СУММА К ОПЛАТЕ", "СУММА",
    "БЕЗНАЛИЧНЫМИ", "НАЛИЧНЫМИ", "ЭЛЕКТРОННЫМИ", "КАРТОЙ",
)
# Строки, которые похожи на итог, но итогом не являются. Без этого списка
# разбор радостно принимал за сумму чека НДС, цену литра или сдачу.
_TOTAL_STOP_WORDS = (
    "НДС", "СДАЧА", "ЦЕНА", "ТАРИФ", "СКИДКА", "БОНУС", "ПРЕДОПЛАТА",
    "ОСТАТОК", "БАЛАНС", "ИНН", "СМЕНА", "ЧЕК",
)
_VOLUME_WORDS = ("ОБЪЕМ", "ОБЪЁМ", "КОЛИЧЕСТВО", "КОЛ-ВО", "КОЛВО", "ЛИТРОВ", "ОТПУЩЕНО")

# Порядок веток важен: сначала «1 234,56» с разделителем тысяч, потом обычное
# «2758,25». Если поставить наоборот, от «2758,25» останется «275» — первая
# ветка съест три цифры и остановится.
_NUM_RE = re.compile(
    r"(?<!\d)\d{1,3}(?:[  \u00a0]\d{3})+(?:[.,-]\d{1,2})?(?!\d)"
    r"|(?<!\d)\d+(?:[.,-]\d{1,2})?(?!\d)"
)
# Одометр: только отдельно стоящее длинное число. Без границ «12:45 95 21»
# склеивалось в 459521 — то есть в пробег, которого не было.
_ODO_RE = re.compile(r"(?<!\d)(?:\d{1,3}(?:[  \u00a0]\d{3})+|\d{4,7})(?!\d)")


def _normalize(text: str) -> str:
    """Убрать разметку и неразрывные пробелы, привести к верхнему регистру."""
    text = text.replace("\u00a0", " ").replace("\u202f", " ")
    text = re.sub(r"[*_`#>|]+", " ", text)       # markdown от LlamaParse
    return text.upper()


def _numbers(line: str) -> list[Decimal]:
    out: list[Decimal] = []
    for token in _NUM_RE.findall(line):
        cleaned = token.replace(" ", "").replace("\u00a0", "")
        # «4250-00» на чеках означает 4250.00, а не диапазон
        cleaned = cleaned.replace("-", ".").replace(",", ".")
        if cleaned.count(".") > 1:
            head, _, tail = cleaned.rpartition(".")
            cleaned = head.replace(".", "") + "." + tail
        try:
            out.append(Decimal(cleaned))
        except InvalidOperation:
            continue
    return out


def _amount_from_lines(lines: list[str]) -> Decimal | None:
    for word in _TOTAL_WORDS:
        for index, line in enumerate(lines):
            if word not in line:
                continue
            if any(stop in line for stop in _TOTAL_STOP_WORDS):
                continue
            candidates = _numbers(line.split(word, 1)[1]) or _numbers(line)
            # в столбик: число может стоять на следующей строке
            if not candidates and index + 1 < len(lines):
                candidates = _numbers(lines[index + 1])
            plausible = [c for c in candidates if Decimal("1") <= c <= Decimal("999999")]
            if plausible:
                return max(plausible)
    return None


def _litres_from_lines(lines: list[str]) -> Decimal | None:
    for line in lines:
        if any(stop in line for stop in ("ЦЕНА", "НДС")):
            continue
        # «42,50 Л» / «42.5 Л.» / «42,50 ЛИТРА»
        match = re.search(r"(\d+[.,]\d{1,2}|\d+)\s*Л(?:ИТР\w*)?\b", line)
        if match:
            value = _numbers(match.group(1))
            if value and Decimal("1") <= value[0] <= Decimal("2000"):
                return value[0]
    for line in lines:
        if not any(word in line for word in _VOLUME_WORDS):
            continue
        for value in _numbers(line):
            if Decimal("1") <= value <= Decimal("2000"):
                return value
    return None


# -------------------------------------------------------------------------
# QR-код фискального чека — самый точный источник суммы, какой вообще есть
#
# В QR на кассовом чеке лежит НЕ ссылка, а строка фискальных данных ФНС:
#   t=20260827T1230&s=2758.25&fn=9960440301234567&i=12345&fp=1234567890&n=1
# где s — сумма чека в рублях. Поэтому телефон и не открывает по нему сайт,
# а предлагает поиск: это просто текст, а не адрес.
#
# Если сумма пришла отсюда — распознавать её не нужно, она уже точная до
# копейки. Литров в QR нет: их всё равно берём из текста чека.
# -------------------------------------------------------------------------
_QR_FIELD_RE = re.compile(r"(?:^|[&?\s])([tsfnipd]{1,2})=([^&\s]+)", re.IGNORECASE)


def parse_fiscal_qr(text: str | None) -> ReceiptReading | None:
    """Достать сумму из строки QR-кода фискального чека.

    Возвращает None, если это не фискальная строка. Проверяем не только `s`:
    случайный текст с «s=» суммой чека не является, поэтому требуем ещё и
    признаки фискального документа (fn/i/fp).
    """
    if not text:
        return None
    fields = {key.lower(): value for key, value in _QR_FIELD_RE.findall(text)}
    if "s" not in fields:
        return None
    # без реквизитов фискального документа это не чек, а совпадение
    if not {"fn", "i", "fp"} & set(fields):
        return None
    amount = _to_decimal(fields["s"])
    if amount is None or not (Decimal("1") <= amount <= Decimal("999999")):
        return None
    return ReceiptReading(amount_rub=amount, liters=None, raw=text[:500])


def parse_receipt_text(text: str | None) -> ReceiptReading | None:
    """Достать итоговую сумму и литры из распознанного текста чека.

    Порядок важен: сначала пробуем QR — там сумма ТОЧНАЯ, ошибиться нечем.
    И только если QR не нашёлся, читаем текст глазами разбора.

    Возвращает None, если ничего правдоподобного не нашлось — тогда бот
    спросит сумму у водителя вручную, как и без OCR.
    """
    if not text:
        return None
    from_qr = parse_fiscal_qr(text)
    lines = [line.strip() for line in _normalize(text).splitlines() if line.strip()]
    if from_qr is not None:
        # литры в QR не лежат — доберём из текста, если он распознался
        return ReceiptReading(
            amount_rub=from_qr.amount_rub,
            liters=_litres_from_lines(lines),
            raw=from_qr.raw,
        )
    if not lines:
        return None
    amount = _amount_from_lines(lines)
    litres = _litres_from_lines(lines)
    if amount is None and litres is None:
        return None
    return ReceiptReading(amount_rub=amount, liters=litres, raw=text[:500])


def parse_odometer_text(text: str | None) -> OdometerReading | None:
    """Достать пробег из распознанного текста приборной панели.

    Берём самое длинное число: одометр — самое многозначное на панели.
    Время, дату и мелкие числа отбрасываем.
    """
    if not text:
        return None
    best: int | None = None
    for token in _ODO_RE.findall(_normalize(text)):
        digits = re.sub(r"\D", "", token)
        if not (4 <= len(digits) <= 7):
            continue
        value = int(digits)
        if 0 <= value <= 9_999_999 and (best is None or value > best):
            best = value
    if best is None:
        return None
    return OdometerReading(km=best, raw=text[:200])


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
