"""
Единая точка запуска для Railway.

Один GitHub-репозиторий используется двумя Railway-сервисами:
- основной `condur` запускает ботов + веб;
- `egts-receiver` запускает только TCP-приёмник Stavtrack.

Railway читает `railway.json` для обоих сервисов, поэтому команду запуска
держим общей, а роль выбираем через SERVICE_ROLE.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def service_role() -> str:
    role = os.environ.get("SERVICE_ROLE", "").strip().lower()
    if role:
        return role

    service_name = os.environ.get("RAILWAY_SERVICE_NAME", "").strip().lower()
    if "egts" in service_name or "gps" in service_name:
        return "egts"
    return "main"


def run_migrations() -> None:
    if os.environ.get("RUN_MIGRATIONS", "true").strip().lower() in {"0", "false", "off", "no"}:
        logger.info("RUN_MIGRATIONS выключен, Alembic пропускаем.")
        return
    subprocess.run(["alembic", "upgrade", "head"], check=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    role = service_role()
    logger.info("Railway service role: %s", role)

    if role in {"egts", "gps", "telemetry"}:
        from app.telemetry.egts_receiver import main as egts_main

        egts_main()
        return

    if role in {"main", "web", "bot", "condur"}:
        run_migrations()
        from app.main import main as app_main

        asyncio.run(app_main())
        return

    raise RuntimeError(
        "Неизвестный SERVICE_ROLE. Используйте 'main' для condur или 'egts' для GPS-приёмника."
    )


if __name__ == "__main__":
    main()
