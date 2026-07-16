"""
EGTS/TCP-приёмник ретрансляции Stavtrack.

Запускается отдельным Railway-сервисом:
    python -m app.telemetry.egts_receiver

Этап 2 (текущий): пакеты разбираются парсером app/telemetry/egts.py —
сохраняем сырьё + нормализованные GPS-точки (координаты, скорость, зажигание,
пробег), обновляем последнее состояние машины и отвечаем трекеру
EGTS_PT_RESPONSE, чтобы Stavtrack не рвал связь и не слал повторы.

Привязка к машине: OID из пакета == vehicles.stavtrack_object_id.
  - ровно одна активная машина → точки пишутся с owner_id/vehicle_id;
  - ни одной → пакет сохраняем со статусом ignored (чужой/ещё не привязан);
  - несколько (один ID у разных владельцев) → ignored + 'ambiguous terminal id',
    точки НЕ пишем никому — иначе чужие данные попали бы в оба кабинета.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services import telemetry_service
from app.telemetry import egts

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


async def _process_packet(
    payload: bytes, *, peer_host: str | None, peer_port: int | None
) -> bytes | None:
    """Разбирает пакет, пишет в БД, возвращает байты ACK (или None).

    Импорты БД — внутри функции: чистые тесты парсера не требуют SQLAlchemy.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.database import async_session
    from app.models import (
        Vehicle,
        VehicleState,
        VehicleTelemetryPoint,
        VehicleTelemetryRawPacket,
    )

    parsed: egts.ParsedPacket | None = None
    parse_error: str | None = None
    try:
        parsed = egts.parse_packet(payload)
    except egts.EgtsParseError as exc:
        parse_error = str(exc)

    oid = parsed.object_id if parsed else None
    terminal_id = str(oid) if oid is not None else None

    async with async_session() as session:
        vehicle: Vehicle | None = None
        skip_reason: str | None = None
        if parsed is not None and terminal_id is not None:
            matches = (
                await session.execute(
                    select(Vehicle).where(
                        Vehicle.stavtrack_object_id == terminal_id,
                        Vehicle.is_active.is_(True),
                    )
                )
            ).scalars().all()
            if len(matches) == 1:
                vehicle = matches[0]
            elif len(matches) > 1:
                skip_reason = "ambiguous terminal id (несколько машин с этим Stavtrack ID)"
            else:
                skip_reason = "unknown terminal id (машина не привязана)"

        # Чужой/непривязанный трекер: в БАЗУ НЕ ПИШЕМ (иначе она пухнет от
        # мусора), только строка в лог. ACK всё равно отправим — пусть
        # Stavtrack не ретраит.
        if parsed is not None and vehicle is None:
            logger.info(
                "EGTS skipped (%s): terminal=%s bytes=%s",
                skip_reason or "no terminal id", terminal_id, len(payload),
            )
            return egts.build_response(parsed)

        raw = VehicleTelemetryRawPacket(
            protocol="egts",
            source="stavtrack",
            peer_host=peer_host,
            peer_port=peer_port,
            terminal_id=terminal_id,
            vehicle_id=vehicle.id if vehicle else None,
            payload=payload,
            payload_size=len(payload),
            parse_status="failed" if parse_error else "parsed",
            parse_error=parse_error,
        )
        session.add(raw)
        await session.flush()

        points_saved = 0
        last_good: VehicleTelemetryPoint | None = None   # достоверная точка
        last_any: VehicleTelemetryPoint | None = None
        if parsed is not None and vehicle is not None:
            for rec in parsed.records:
                for pos in rec.positions:
                    # «Нулевой остров»: при потере GPS часть трекеров шлёт (0,0).
                    zeroish = abs(pos.latitude) < 0.001 and abs(pos.longitude) < 0.001
                    good = pos.is_valid and not zeroish
                    point = VehicleTelemetryPoint(
                        raw_packet_id=raw.id,
                        owner_id=vehicle.owner_id,
                        vehicle_id=vehicle.id,
                        terminal_id=terminal_id,
                        observed_at=pos.navigation_time,
                        latitude=Decimal(str(pos.latitude)),
                        longitude=Decimal(str(pos.longitude)),
                        speed_kmh=Decimal(str(pos.speed_kmh)),
                        course=Decimal(pos.course),
                        ignition=pos.ignition,
                        mileage_km=Decimal(str(pos.odometer_km)),
                        is_valid=good,
                        anomaly_reason=None if good else "нет достоверных координат (GPS)",
                    )
                    session.add(point)
                    points_saved += 1
                    last_any = point
                    if good:
                        last_good = point

            if last_any is not None:
                await session.flush()  # нужны id точек
                previous_state = await session.get(VehicleState, vehicle.id)
                motion_status = telemetry_service.vehicle_motion_status(
                    last_any.speed_kmh, last_any.ignition
                )
                if (
                    previous_state is not None
                    and previous_state.motion_status == motion_status
                    and previous_state.motion_since_at is not None
                ):
                    motion_since_at = previous_state.motion_since_at
                else:
                    motion_since_at = last_any.observed_at
                if last_good is not None:
                    # Полное обновление состояния достоверной точкой.
                    values = dict(
                        vehicle_id=vehicle.id,
                        terminal_id=terminal_id,
                        last_point_id=last_good.id,
                        last_seen_at=last_good.observed_at,
                        latitude=last_good.latitude,
                        longitude=last_good.longitude,
                        speed_kmh=last_good.speed_kmh,
                        ignition=last_good.ignition,
                        motion_status=motion_status,
                        motion_since_at=motion_since_at,
                        is_valid=True,
                        anomaly_reason=None,
                    )
                    update_cols = (
                        "terminal_id", "last_point_id", "last_seen_at", "latitude",
                        "longitude", "speed_kmh", "ignition", "motion_status",
                        "motion_since_at", "is_valid", "anomaly_reason",
                    )
                else:
                    # Только недостоверные точки: машину «видели» (обновляем время,
                    # зажигание и пометку), но координаты НЕ трогаем — иначе метка
                    # улетает в «нулевой остров» у берегов Африки.
                    values = dict(
                        vehicle_id=vehicle.id,
                        terminal_id=terminal_id,
                        last_seen_at=last_any.observed_at,
                        ignition=last_any.ignition,
                        motion_status=motion_status,
                        motion_since_at=motion_since_at,
                        is_valid=False,
                        anomaly_reason="нет достоверных координат (GPS)",
                    )
                    update_cols = (
                        "terminal_id", "last_seen_at", "ignition", "motion_status",
                        "motion_since_at", "is_valid", "anomaly_reason",
                    )
                stmt = pg_insert(VehicleState).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[VehicleState.vehicle_id],
                    set_={col: getattr(stmt.excluded, col) for col in update_cols},
                )
                await session.execute(stmt)

        await session.commit()
        # Диагностика для калибровки датчиков (зажигание/напряжение): типы
        # подзаписей, которые пока не разбираем, напряжение борта и полный hex.
        unknown_types = sorted({
            t for rec in (parsed.records if parsed else []) for t in rec.unknown_subrecords
        })
        ext_pos_payloads = sorted({
            pos.ext_pos_data.hex(" ")
            for rec in (parsed.records if parsed else [])
            for pos in rec.positions
            if pos.ext_pos_data
        })
        state_v = next(
            (rec.state.main_power_v for rec in (parsed.records if parsed else [])
             if rec.state is not None),
            None,
        )
        logger.info(
            "EGTS packet id=%s status=%s terminal=%s vehicle=%s points=%s bytes=%s "
            "power=%sV ext_pos=%s unknown_sr=%s hex=%s",
            raw.id, raw.parse_status, terminal_id,
            vehicle.license_plate if vehicle else "—",
            points_saved, len(payload),
            state_v if state_v is not None else "—",
            ext_pos_payloads or "—",
            unknown_types or "—",
            _preview_hex(payload, limit=96),
        )

    # ACK шлём на любой корректно разобранный транспортный пакет — иначе
    # Stavtrack продолжит рвать связь и слать повторы.
    return egts.build_response(parsed) if parsed is not None else None


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    config: ReceiverConfig,
) -> None:
    peer_host, peer_port = _peer_parts(writer.get_extra_info("peername"))
    peer_label = f"{peer_host}:{peer_port}" if peer_host and peer_port else str(peer_host or "unknown")
    logger.info("EGTS connection opened: %s", peer_label)

    buffer = b""
    desynced = False
    try:
        while not desynced:
            try:
                chunk = await asyncio.wait_for(
                    reader.read(config.max_packet_bytes),
                    timeout=config.idle_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.info("EGTS connection idle timeout: %s", peer_label)
                break

            if not chunk:
                break
            buffer += chunk
            if len(buffer) > config.max_packet_bytes * 4:
                logger.warning("EGTS buffer overflow from %s, dropping connection", peer_label)
                break

            # В буфере может лежать несколько пакетов (или пол-пакета).
            while True:
                # Рассинхронизация потока (мусор/половина пакета в начале):
                # чинить бесполезно — рвём соединение, ретранслятор
                # переподключится и дошлёт пакеты заново. Иначе приёмник
                # молча ждёт «пакет» выдуманной длины и глотает всё подряд.
                head_err = egts.header_error(buffer)
                if head_err is not None:
                    logger.warning(
                        "EGTS поток рассинхронизирован от %s (%s, буфер %s байт) — "
                        "закрываем соединение",
                        peer_label, head_err, len(buffer),
                    )
                    desynced = True
                    break
                need = egts.packet_length(buffer)
                if need is None:
                    break
                if need > config.max_packet_bytes:
                    logger.warning(
                        "EGTS пакет заявляет %s байт (> лимита %s) от %s — "
                        "закрываем соединение",
                        need, config.max_packet_bytes, peer_label,
                    )
                    desynced = True
                    break
                if len(buffer) < need:
                    break
                packet, buffer = buffer[:need], buffer[need:]
                try:
                    ack = await _process_packet(packet, peer_host=peer_host, peer_port=peer_port)
                except Exception:
                    logger.exception("Не удалось обработать EGTS-пакет от %s", peer_label)
                    continue
                if ack:
                    writer.write(ack)
                    await writer.drain()
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
    # stream=sys.stdout: иначе INFO-строки уходят в stderr и Railway
    # показывает их красным как ошибки.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(serve())


if __name__ == "__main__":
    main()
