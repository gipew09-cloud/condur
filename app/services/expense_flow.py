"""
Расход и SOS водителя — ОДНА логика для Telegram-бота и приложения.

Как `shift_flow` и `trip_flow`: бот и телефон зовут одни и те же функции,
поэтому владелец получает одинаковые сообщения и кнопки, откуда бы водитель
ни нажал. Поведение бота закреплено тестами (`tests/test_expense_flow.py`)
ДО переноса сюда.

⚠️ Водитель не видит решения владельца по расходу (решение владельца
16.09.2026): здесь нет ничего, что отдавало бы телефону статус «отклонён».
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import keyboards as kb
from app.bots import messages as msg
from app.bots.notifications import notify_owner
from app.database import async_session
from app.models import Driver, Expense, Owner, Shift, Trip, Vehicle
from app.services import expense_service, receipt_ocr, shift_service, trip_service
from app.services.event_service import log_event
from app.services.textsanitize import clean_user_text

logger = logging.getLogger(__name__)

# Больше не бывает: поле суммы в базе — до 99 999 999,99; миллион с запасом
# покрывает любой ремонт, а опечатку «450000» вместо «4500» — нет, это решает
# владелец кнопкой «Изменить».
MAX_AMOUNT = Decimal("1000000")
MAX_DESCRIPTION = 200


class Refused(Exception):
    """Расход не принят — текст для водителя."""


def categories() -> list[dict]:
    """Категории в том же порядке, что кнопки в боте."""
    return [
        {"code": code, "label": expense_service.CATEGORY_LABELS[code]}
        for code in expense_service.VALID_CATEGORIES
    ]


def parse_amount(value) -> Decimal | None:
    """«4 500,50» → 4500.50. Ничего не угадываем: не число — None."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", ".").replace(" ", "").replace(" ", "")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    if not amount.is_finite():
        return None
    return amount


async def ensure_can_submit(session: AsyncSession, driver: Driver) -> None:
    """Не больше десяти расходов ждут решения — защита от спама кнопкой."""
    pending = await expense_service.count_pending_expenses(session, driver.id)
    if pending >= expense_service.MAX_PENDING_PER_DRIVER:
        raise Refused(
            f"⚠️ У тебя уже {pending} расходов ожидают проверки владельцем.\n"
            "Подожди, пока он их рассмотрит, и попробуй снова."
        )


@dataclass
class Submitted:
    expense: Expense
    shift: Shift | None
    trip: Trip | None

    @property
    def category(self) -> str:
        return self.expense.category

    @property
    def amount(self) -> Decimal:
        return self.expense.amount_rub


async def submit(
    session: AsyncSession,
    *,
    driver: Driver,
    category: str,
    amount: Decimal,
    description: str | None,
    receipt_ref: str | None,
    source: str | None = None,
) -> Submitted:
    """Записать расход и событие (коммит — на вызывающем).

    Смена не обязательна (Правка 3): вне смены расход без смены и рейса.
    """
    if category not in expense_service.VALID_CATEGORIES:
        raise Refused("Неизвестная категория расхода. Обновите приложение.")
    # Защита от двойного нажатия: за последнюю минуту уже есть точно такой же
    # расход (сумма + категория) — скорее всего лаг сети.
    if await expense_service.is_duplicate_expense(
        session, driver_id=driver.id, category=category, amount_rub=amount,
    ):
        raise Refused(
            f"⚠️ Расход {expense_service.CATEGORY_LABELS[category]} {amount:.0f} ₽ "
            "уже был добавлен только что.\n"
            "Если это другой расход — подожди минуту и попробуй снова."
        )
    shift = await shift_service.get_active_shift(session, driver.id)
    trip = await trip_service.get_active_trip(session, shift.id) if shift else None
    expense = await expense_service.create_expense(
        session,
        owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id if shift else None, trip_id=trip.id if trip else None,
        category=category, amount_rub=amount,
        receipt_photo_id=receipt_ref,
        description=description,
    )
    await session.flush()
    payload = {"expense_id": expense.id, "category": category, "amount": str(amount)}
    if source:
        payload["source"] = source
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id if shift else None, trip_id=trip.id if trip else None,
        event_type="expense_submitted",
        payload=payload,
    )
    return Submitted(expense=expense, shift=shift, trip=trip)


def liters_hint(category: str, amount: Decimal) -> str:
    """Подсказка по литрам — только для топлива."""
    if category != "fuel":
        return ""
    return f" (~{trip_service.liters_from_rub(amount):.0f} л)"


def driver_text(category: str, amount: Decimal) -> str:
    """Что водитель видит в боте после отправки."""
    if category != "fuel":
        return msg.EXPENSE_SUBMITTED
    return msg.EXPENSE_SUBMITTED + f"\n\nТопливо: {amount:.0f} ₽{liters_hint(category, amount)}"


def owner_caption(driver_name: str, category: str, amount: Decimal, description: str | None) -> str:
    caption = msg.NOTIFY_EXPENSE.format(
        driver=driver_name,
        category=expense_service.CATEGORY_LABELS[category],
        amount=f"{amount:.0f}" + liters_hint(category, amount),
    )
    if description:
        caption += f"\nОписание: {description}"
    return caption


def owner_markup(expense_id: int):
    return kb.expense_decision_keyboard(expense_id)


def clean_description(value) -> str | None:
    text = clean_user_text(value if isinstance(value, str) else None)
    return text[:MAX_DESCRIPTION] or None


async def receipt_followup(
    *,
    owner_bot,
    owner_id: int,
    driver_name: str,
    typed: Decimal | None,
    image_bytes: bytes,
) -> None:
    """Распознать чек и, если сумма разошлась, догнать владельца сообщением.

    Работает уже после ответа водителю, поэтому:
    — своя сессия БД: та, что была у обработчика, к этому моменту закрыта;
    — ничего не бросает наружу: сбой распознавания не должен всплывать нигде,
      расход уже сохранён с суммой водителя.

    Распознанная сумма НЕ подменяет введённую: OCR ошибётся — расход уедет с
    неверной цифрой, и никто не заметит. Решает владелец.
    """
    try:
        reading = await receipt_ocr.recognize(image_bytes)
        if not (reading and reading.amount_rub):
            logger.info("OCR чека: сумма не распозналась, оставляем введённую водителем")
            return
        logger.info(
            "OCR чека: распознано %s ₽, водитель ввёл %s ₽",
            reading.amount_rub, typed if typed is not None else "—",
        )
        if typed is not None and abs(reading.amount_rub - typed) <= Decimal("1"):
            return  # сходится — владельца не дёргаем

        text = (
            f"🧾 Чек от <b>{driver_name}</b>: на чеке распознано "
            f"<b>{reading.amount_rub:.2f} ₽</b>"
        )
        text += f", водитель ввёл {typed:.0f} ₽. Проверьте." if typed is not None else "."
        async with async_session() as ocr_session:
            owner = await ocr_session.get(Owner, owner_id)
            if owner is not None:
                await notify_owner(owner_bot, ocr_session, owner, text)
                await ocr_session.commit()
    except Exception as exc:  # noqa: BLE001 — распознавание не критично
        logger.warning("OCR чека не отработал: %s", exc)


# ------------------------------------------------------------------ SOS
@dataclass
class SosSent:
    state: str
    plate: str | None
    shift: Shift | None
    trip: Trip | None

    def owner_text(self, driver: Driver) -> str:
        return msg.NOTIFY_SOS.format(
            driver=driver.full_name,
            plate=self.plate or "—",
            phone=driver.phone or "—",
            state=self.state,
        )


async def send_sos(session: AsyncSession, *, driver: Driver, source: str | None = None) -> SosSent:
    """Записать SOS (коммит — на вызывающем) и собрать, что сказать владельцу."""
    shift = await shift_service.get_active_shift(session, driver.id)
    trip = await trip_service.get_active_trip(session, shift.id) if shift else None
    vehicle = await session.get(Vehicle, shift.vehicle_id) if shift else None

    if trip is not None:
        status = {"created": "создан", "in_transit": "в пути", "unloading": "на выгрузке"}.get(
            trip.status, trip.status
        )
        state = f"рейс {status}, {trip.origin or '—'} → {trip.destination or '—'}"
    elif shift is not None:
        state = "в смене, без активного рейса"
    else:
        state = "вне смены"

    payload = {"state": state}
    if source:
        payload["source"] = source
    await log_event(
        session,
        owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id if shift else None,
        trip_id=trip.id if trip else None,
        event_type="sos",
        payload=payload,
    )
    return SosSent(
        state=state,
        plate=vehicle.license_plate if vehicle else None,
        shift=shift,
        trip=trip,
    )
