"""
Диагностический EGTS/TCP-приёмник для ретрансляции Stavtrack.

Запускается отдельным Railway-сервисом:
    python -m app.telemetry.egts_receiver

Это не второй Telegram-бот. Процесс только слушает TCP-порт и сохраняет
сырые пакеты в Postgres. После получения реальных пакетов добавим парсер EGTS
и ACK под фактическое поведение Stavtrack.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiverConfig:
    host: str
    port: int
    max_packet_bytes: int
    idle_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "ReceiverConfig":
        return cls(
            host=os.environ.get("EGTS_HOST", "0.0.0.0"),
            port=_env_int("EGTS_PORT", _env_int("PORT", 9000), minimum=1, maximum=65535),
            max_packet_bytes=_env_int("EGTS_MAX_PACKET_BYTES", 65536, minimum=256),
            idle_timeout_seconds=_env_int("EGTS_IDLE_TIMEOUT_SECONDS", 120, minimum=10),
        )


def _env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            logger.warning("ENV %s=%r не число, используем %s", name, raw, default)
            value = default

    if minimum is not None:
        value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _peer_parts(peername: Any) -> tuple[str | None, int | None]:
    if isinstance(peername, tuple) and len(peername) >= 2:
        return str(peername[0]), int(peername[1])
    if peername:
        return str(peername), None
    return None, None


def _preview_hex(payload: bytes, *, limit: int = 24) -> str:
    return payload[:limit].hex(" ")


async def _save_raw_packet(payload: bytes, *, peer_host: str | None, peer_port: int | None) -> int | None:
    # Импорт внутри функции: GPS-worker не тянет SQLAlchemy при простом импорте
    # модуля, а тесты чистых helper-ов не требуют установленной БД.
    from app.database import async_session
    from app.models import VehicleTelemetryRawPacket

    async with async_session() as session:
        packet = VehicleTelemetryRawPacket(
            protocol="egts",
            source="stavtrack",
            peer_host=peer_host,
            peer_port=peer_port,
            payload=payload,
            payload_size=len(payload),
            parse_status="raw",
        )
        session.add(packet)
        await session.commit()
        return packet.id


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    config: ReceiverConfig,
) -> None:
    peer_host, peer_port = _peer_parts(writer.get_extra_info("peername"))
    peer_label = f"{peer_host}:{peer_port}" if peer_host and peer_port else str(peer_host or "unknown")
    logger.info("EGTS connection opened: %s", peer_label)

    try:
        while True:
            try:
                payload = await asyncio.wait_for(
                    reader.read(config.max_packet_bytes),
                    timeout=config.idle_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.info("EGTS connection idle timeout: %s", peer_label)
                break

            if not payload:
                break

            try:
                packet_id = await _save_raw_packet(payload, peer_host=peer_host, peer_port=peer_port)
            except Exception:
                logger.exception("Не удалось сохранить EGTS-пакет от %s", peer_label)
                continue

            logger.info(
                "EGTS raw saved: id=%s peer=%s bytes=%s preview=%s",
                packet_id,
                peer_label,
                len(payload),
                _preview_hex(payload),
            )
    finally:
        writer.close()
        await writer.wait_closed()
        logger.info("EGTS connection closed: %s", peer_label)


async def serve(config: ReceiverConfig | None = None) -> None:
    cfg = config or ReceiverConfig.from_env()
    server = await asyncio.start_server(
        lambda reader, writer: handle_client(reader, writer, cfg),
        host=cfg.host,
        port=cfg.port,
    )

    sockets = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    logger.info("EGTS receiver listening on %s", sockets)
    async with server:
        await server.serve_forever()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(serve())


if __name__ == "__main__":
    main()
