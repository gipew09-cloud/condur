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
import sys

logger = logging.getLogger(__name__)


def service_role() -> str:
    role = os.environ.get("SERVICE_ROLE", "").strip().lower()
    if role:
        return role

    service_name = os.environ.get("RAILWAY_SERVICE_NAME", "").strip().lower()
    if "egts" in service_name or "gps" in service_name:
        return "egts"
    return "main"


def service_role_debug() -> dict[str, str]:
    service_name = os.environ.get("RAILWAY_SERVICE_NAME", "").strip()
    explicit_role = os.environ.get("SERVICE_ROLE", "").strip()
    resolved_role = service_role()
    if explicit_role:
        source = "SERVICE_ROLE"
    elif "egts" in service_name.lower() or "gps" in service_name.lower():
        source = "RAILWAY_SERVICE_NAME"
    else:
        source = "default"
    return {
        "service_name": service_name or "unknown",
        "explicit_role": explicit_role or "not set",
        "resolved_role": resolved_role,
        "source": source,
    }


def run_migrations() -> None:
    if os.environ.get("RUN_MIGRATIONS", "true").strip().lower() in {"0", "false", "off", "no"}:
        logger.info("RUN_MIGRATIONS выключен, Alembic пропускаем.")
        return
    subprocess.run(["alembic", "upgrade", "head"], check=True)


def main() -> None:
    # stream=sys.stdout: иначе INFO-строки уходят в stderr и Railway
    # показывает их красным как ошибки.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    debug = service_role_debug()
    role = debug["resolved_role"]
    logger.info(
        "Railway service role: %s (source=%s, service=%s, SERVICE_ROLE=%s)",
        role, debug["source"], debug["service_name"], debug["explicit_role"],
    )

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
