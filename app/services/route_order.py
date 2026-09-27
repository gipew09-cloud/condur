"""Порядок складов и маршрутов — один на сайт, бот и приложение водителя.

Владелец расставляет на сайте (/routes) и маршруты внутри склада, и сами
склады (27.09.2026: «нужно, чтобы склады тоже можно было перетаскивать вверх
вниз»). Водитель видит тот же порядок в боте и в приложении.

⚠️ Отдельной колонки под порядок складов нет — порядок живёт в том же
`RouteTemplate.sort_order`, диапазонами: у склада №0 маршруты 10, 20, 30…,
у склада №1 — 10010, 10020…, у склада №2 — 20010… Место склада — это
`sort_order // BAND` его маршрутов. Так не нужна новая колонка и выкладка
остаётся простой: старые маршруты (порядок 0…200) все на месте №0, и склады
между собой по-прежнему идут по алфавиту, пока владелец их не переставит.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from app.services.textsanitize import origin_key

BAND = 10_000   # диапазон одного склада
STEP = 10       # шаг внутри склада


class _Template(Protocol):
    origin: str | None
    destination: str | None
    sort_order: int | None


def folder_rank(template: _Template) -> int:
    return (template.sort_order or 0) // BAND


def grouped(
    templates: Iterable[_Template],
    key: Callable[[str | None], str] = origin_key,
) -> dict[str, list]:
    """Маршруты папками по складам в порядке владельца.

    Маршруты должны прийти уже упорядоченными (`sort_order`, `destination`,
    `name` — как их берут все места). Склад встаёт по наименьшему месту
    своих маршрутов, при равенстве — по алфавиту ключа. Пустой ключ — мимо.
    """
    folders: dict[str, list] = {}
    rank: dict[str, int] = {}
    for template in templates:
        k = key(template.origin)
        if not k:
            continue
        folders.setdefault(k, []).append(template)
        rank[k] = min(rank.get(k, folder_rank(template)), folder_rank(template))
    return {k: folders[k] for k in sorted(folders, key=lambda k: (rank[k], k))}


def renumber(folders: list[list]) -> None:
    """Записать порядок как на экране: склад i — диапазон i·BAND, маршрут
    j внутри — +(j+1)·STEP. Пересобираем целиком: надёжнее обмена значениями,
    если у части маршрутов порядок одинаковый (например, все нули)."""
    for i, routes in enumerate(folders):
        for j, template in enumerate(routes):
            template.sort_order = i * BAND + (j + 1) * STEP


def next_place(
    templates: Iterable[_Template],
    folder: str,
    key: Callable[[str | None], str] = origin_key,
) -> int:
    """Место для нового маршрута: в конце своего склада, а новый склад —
    последним. Раньше новый маршрут получал 0 и вставал ПЕРВЫМ в складе."""
    everything = list(templates)
    mine = [t.sort_order or 0 for t in everything if key(t.origin) == folder]
    if mine:
        return max(mine) + STEP
    last = max((folder_rank(t) for t in everything), default=-1)
    return (last + 1) * BAND + STEP
