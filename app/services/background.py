"""Фоновые дела «после ответа»: распознать чек, сверить одометр.

⚠️ Задачу asyncio нельзя просто создать и забыть. Цикл событий держит на неё
только слабую ссылку — сборщик мусора может убрать задачу, не дав ей
доработать, и распознавание чека молча не случится. А упавшая задача пишет
ошибку в журнал когда-нибудь потом, без понятного контекста (разбор
26.09.2026). Здесь задачи держатся до конца, а ошибка сразу пишется в журнал
с названием дела.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

logger = logging.getLogger(__name__)

_RUNNING: set[asyncio.Task] = set()


def spawn(coro: Coroutine, *, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _RUNNING.add(task)
    task.add_done_callback(_finished)
    return task


def _finished(task: asyncio.Task) -> None:
    _RUNNING.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("Фоновое дело «%s» упало", task.get_name(), exc_info=error)
