"""Фоновое дело не теряется и не молчит об ошибке (разбор 26.09.2026)."""
import asyncio
import logging

from app.services import background


def test_упавшее_дело_пишется_в_журнал_с_названием(caplog):
    async def boom():
        raise RuntimeError("распознавание упало")

    async def scenario():
        task = background.spawn(boom(), name="чек из приложения")
        assert task in background._RUNNING
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task not in background._RUNNING

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert "чек из приложения" in caplog.text
