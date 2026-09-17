"""
Одометр с фото: распознать и проверить — одинаково для бота и приложения.

Случай владельца 17.09.2026 (смена 119): водитель прислал в конце смены то же
фото, что утром. Распознавание честно прочитало то же число, пробег вышел
нулём — и никто ничего не сказал, а пробег по GPS на странице смены не
показывался. Теперь:
- одно и то же фото в начале и в конце — владельцу предупреждение сразу;
- распознанное в конце число не больше утреннего — в смену НЕ пишется,
  владельцу предупреждение с пробегом по GPS;
- распознанное вписывается, только если человек ещё ничего не вписал
  (человек главнее машины — правило бота с 2026-08).

Всё здесь работает ПОСЛЕ ответа водителю: распознавание медленное (до
десятков секунд), водителю нельзя стоять и ждать. Наружу ничего не бросаем.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.bots.notifications import notify_owner
from app.config import settings
from app.database import async_session
from app.models import Owner, Shift
from app.services import receipt_ocr, telemetry_service

logger = logging.getLogger(__name__)


def ocr_enabled() -> bool:
    """Распознавать ли одометр: выключатель владельца и наличие ключа."""
    return settings.feature_odometer_ocr and receipt_ocr.is_enabled()


def _km(value) -> str:
    return f"{int(value):,}".replace(",", " ")


async def gps_km_for_shift(session, shift: Shift) -> Decimal | None:
    if shift.started_at is None:
        return None
    return await telemetry_service.gps_mileage_for_period(
        session,
        vehicle_id=shift.vehicle_id,
        start=shift.started_at,
        end=shift.ended_at or datetime.now(timezone.utc),
    )


def _gps_tail(gps_km: Decimal | None) -> str:
    return f" По GPS за смену: <b>{gps_km:.0f} км</b>." if gps_km is not None else ""


def same_reading_warning(shift: Shift, gps_km: Decimal | None) -> str | None:
    """Одометр в начале и в конце одинаковый — пробег 0. Бывает, но чаще это
    одно и то же фото или опечатка."""
    start, end = shift.odometer_start, shift.odometer_end
    if start is None or end is None or start != end:
        return None
    return (
        f"⚠️ Одометр в начале и в конце одинаковый ({_km(start)} км) — пробег 0. "
        f"Возможно, водитель прислал то же фото.{_gps_tail(gps_km)}"
    )


async def followup(
    *,
    owner_bot,
    shift_id: int,
    owner_id: int,
    driver_name: str,
    plate: str,
    closing: bool,
    image_bytes: bytes | None,
    same_photo_as_start: bool = False,
) -> None:
    """Проверить фото одометра и, если можно, вписать распознанное."""
    field = "odometer_end" if closing else "odometer_start"
    try:
        if same_photo_as_start:
            async with async_session() as session:
                shift = await session.get(Shift, shift_id)
                owner = await session.get(Owner, owner_id)
                if shift is None or owner is None:
                    return
                gps_km = await gps_km_for_shift(session, shift)
                await notify_owner(
                    owner_bot, session, owner,
                    f"⚠️ <b>{plate}</b>, {driver_name}: фото одометра в конце смены — "
                    f"то же самое, что в начале. Пробег по фото не посчитать."
                    f"{_gps_tail(gps_km)} Попросите новое фото или впишите одометр "
                    f"кнопкой «Указать пробег».",
                )
                await session.commit()
            return
        # Распознавать ли, решает вызывающий: передал байты — распознаём.
        if not image_bytes:
            return
        reading = await receipt_ocr.recognize_odometer(image_bytes)
        if not (reading and reading.km):
            logger.info("OCR одометра (%s): не распозналось, ждём владельца", field)
            return
        async with async_session() as session:
            shift = await session.get(Shift, shift_id)
            owner = await session.get(Owner, owner_id)
            if shift is None:
                return
            if getattr(shift, field) is not None:
                logger.info("OCR одометра (%s): уже вписано, не трогаем", field)
                return
            when = "в конце смены" if closing else "в начале смены"
            start = shift.odometer_start
            if closing and start is not None and reading.km <= start:
                gps_km = await gps_km_for_shift(session, shift)
                if owner is not None:
                    await notify_owner(
                        owner_bot, session, owner,
                        f"⚠️ <b>{plate}</b>, {driver_name}: на фото одометра {when} "
                        f"<b>{_km(reading.km)} км</b> — не больше, чем в начале "
                        f"({_km(start)} км). Похоже на старое фото, в смену не вписал."
                        f"{_gps_tail(gps_km)} Впишите одометр кнопкой «Указать пробег».",
                    )
                await session.commit()
                return
            setattr(shift, field, reading.km)
            if owner is not None:
                text = (
                    f"🤖 Одометр {when} — <b>{plate}</b>, {driver_name}: "
                    f"<b>{_km(reading.km)} км</b>. Вписал автоматически с фото. "
                    f"Неверно — поправьте кнопкой «Указать пробег» под фото."
                )
                if closing and start is not None:
                    gps_km = await gps_km_for_shift(session, shift)
                    if gps_km is not None:
                        text += "\n" + telemetry_service.format_mileage_comparison(
                            reading.km - start, gps_km
                        )
                await notify_owner(owner_bot, session, owner, text)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — проверка не критична
        logger.warning("Проверка одометра (%s) не отработала: %s", field, exc)
