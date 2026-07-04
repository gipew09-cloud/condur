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
