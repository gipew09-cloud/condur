"""
Геокодинг адресов РЦ через Nominatim (OpenStreetMap).

Бесплатно и без API-ключей. Правила сервиса: не чаще 1 запроса в секунду
и осмысленный User-Agent. Мы геокодим справочник РЦ по кнопке владельца
один раз — это единицы запросов, политику соблюдаем (пауза между запросами).

aiohttp импортируется лениво внутри функций: в локальном тест-окружении
(Python 3.9, без зависимостей ботов) модуль должен импортироваться ради
чистой функции parse_nominatim_response.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
YANDEX_GEOCODER_URL = "https://geocode-maps.yandex.ru/1.x/"
USER_AGENT = "condur-tms/1.0 (fleet TMS cabinet; github.com/gipew09-cloud/condur)"
REQUEST_GAP_SECONDS = 1.1  # политика Nominatim: максимум 1 запрос в секунду


def parse_nominatim_response(items: object) -> tuple[Decimal, Decimal] | None:
    """Достать (широта, долгота) из ответа Nominatim. None — не нашли."""
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    if not isinstance(first, dict):
        return None
    try:
        lat = Decimal(str(first["lat"]))
        lon = Decimal(str(first["lon"]))
    except (KeyError, TypeError, InvalidOperation):
        return None
    # «нулевой остров» и мусор не принимаем
    if abs(lat) < Decimal("0.001") and abs(lon) < Decimal("0.001"):
        return None
    return lat, lon


async def geocode_address(
    http: "aiohttp.ClientSession", address: str
) -> tuple[Decimal, Decimal] | None:
    """Один адрес → координаты или None (не нашли / сервис недоступен)."""
    import aiohttp

    params = {
        "q": address,
        "format": "json",
        "limit": "1",
        "countrycodes": "ru",
        "accept-language": "ru",
    }
    try:
        async with http.get(
            NOMINATIM_URL,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning("Nominatim %s для «%s»", resp.status, address)
                return None
            return parse_nominatim_response(await resp.json(content_type=None))
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("Nominatim недоступен для «%s»: %s", address, exc)
        return None


async def geocode_many(
    addresses: list[str],
) -> list[tuple[Decimal, Decimal] | None]:
    """Геокодировать пачку адресов с паузой между запросами (политика сервиса)."""
    import aiohttp

    results: list[tuple[Decimal, Decimal] | None] = []
    async with aiohttp.ClientSession() as http:
        for i, address in enumerate(addresses):
            if i > 0:
                await asyncio.sleep(REQUEST_GAP_SECONDS)
            results.append(await geocode_address(http, address))
    return results


# =========================================================================
# Обратное геокодирование: координаты → адрес (для метки на мониторинге)
# =========================================================================
def _short_address(full: str | None) -> str | None:
    """Убрать из адреса страну и лишние хвосты — на карточке мало места."""
    if not full:
        return None
    text = str(full).strip()
    for prefix in ("Россия, ", "Russia, ", "Российская Федерация, "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text or None


def parse_yandex_reverse(payload: object) -> str | None:
    """Достать адрес из ответа Геокодера Яндекса. None — не нашли."""
    try:
        members = payload["response"]["GeoObjectCollection"]["featureMember"]  # type: ignore[index]
    except (KeyError, TypeError):
        return None
    if not members:
        return None
    try:
        meta = members[0]["GeoObject"]["metaDataProperty"]["GeocoderMetaData"]
    except (KeyError, TypeError, IndexError):
        return None
    return _short_address(meta.get("text"))


def parse_nominatim_reverse(payload: object) -> str | None:
    """Достать адрес из ответа Nominatim. None — не нашли."""
    if not isinstance(payload, dict):
        return None
    return _short_address(payload.get("display_name"))


async def reverse_geocode(lat: float, lon: float, *, yandex_key: str = "") -> str | None:
    """Координаты → адрес. Яндекс, если задан ключ Геокодера, иначе Nominatim.

    Ключ карт для геокодера НЕ подходит: у Яндекса это разные сервисы и разные
    ключи. Поэтому по умолчанию идём в Nominatim — бесплатно и без ключа.
    """
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession() as http:
            if yandex_key:
                params = {
                    "apikey": yandex_key,
                    "geocode": f"{lon},{lat}",
                    "format": "json",
                    "results": "1",
                    "lang": "ru_RU",
                }
                async with http.get(YANDEX_GEOCODER_URL, params=params, timeout=timeout) as resp:
                    if resp.status == 200:
                        return parse_yandex_reverse(await resp.json(content_type=None))
                    logger.warning("Геокодер Яндекса ответил %s", resp.status)
            params = {
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                "format": "jsonv2",
                "zoom": "18",
                "accept-language": "ru",
            }
            async with http.get(
                NOMINATIM_REVERSE_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    logger.warning("Nominatim reverse ответил %s", resp.status)
                    return None
                return parse_nominatim_reverse(await resp.json(content_type=None))
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("Обратное геокодирование недоступно: %s", exc)
        return None
