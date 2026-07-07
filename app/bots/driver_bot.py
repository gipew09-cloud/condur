"""
Бот ВОДИТЕЛЯ. Этап 2+ — полный цикл смены и рейсов.

Архитектурный принцип:
  FSM — только подсказка для UI. Источник истины — БД.
  В начале каждого хендлера сначала проверяем БД, потом ориентируемся
  на FSM. /status — главная защита: пересоздаёт правильное UI-состояние
  из БД и сбрасывает залипший FSM.
"""
import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import any_state
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import keyboards as kb
from app.bots import messages as msg
from app.bots.notifications import notify_owner, transfer_photo_to_owner
from app.config import settings
from app.bots.states import (
    AddManualShift,
    AddManualTrip,
    DriverTripRevenue,
    EndShift,
    EndShiftLocation,
    EndTripLocation,
    HandedCash,
    NewExpense,
    NewTrip,
    StartShift,
    TripDepartLocation,
    UnloadingLocation,
    UploadWaybill,
)
from app.models import Driver, Expense, Owner, RouteTemplate, Shift, Trip, Vehicle
from app.services import (
    expense_service,
    receipt_ocr,
    salary_service,
    shift_service,
    trip_service,
)
from app.services.cash_pending import PENDING as CASH_PENDING
from app.services.event_service import log_event
from app.services.timeutil import fmt_time, owner_tz

logger = logging.getLogger(__name__)
driver_router = Router()


# =========================================================================
# Хелперы
# =========================================================================
async def _driver_by_telegram(session: AsyncSession, telegram_id: int) -> Driver | None:
    """
    Возвращает активного водителя по telegram_id. Если запись помечена
    is_active=False (владелец удалил на сайте) — возвращаем None, чтобы все
    хендлеры считали такого пользователя «не зарегистрированным».
    """
    result = await session.execute(
        select(Driver).where(
            Driver.telegram_id == telegram_id,
            Driver.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def _driver_by_invite(session: AsyncSession, token: str) -> Driver | None:
    result = await session.execute(select(Driver).where(Driver.invite_token == token))
    return result.scalar_one_or_none()


async def _driver_runtime_state(session: AsyncSession, driver_id: int) -> str:
    shift = await shift_service.get_active_shift(session, driver_id)
    if shift is None:
        return "no_shift"
    trip = await trip_service.get_active_trip(session, shift.id)
    if trip is None:
        return "shift_no_trip"
    return f"trip_{trip.status}"


def _pick_photo_file_id(message: Message) -> str | None:
    if not message.photo:
        return None
    if len(message.photo) >= 2:
        return message.photo[-2].file_id
    return message.photo[-1].file_id


def _parse_int(text: str | None) -> int | None:
    if text is None:
        return None
    cleaned = text.strip().replace(" ", "").replace(" ", "")
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_decimal(text: str | None) -> Decimal | None:
    if text is None:
        return None
    cleaned = text.strip().replace(",", ".").replace(" ", "").replace(" ", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


async def _refresh_ui(message: Message, session: AsyncSession, driver: Driver, text: str) -> None:
    state_code = await _driver_runtime_state(session, driver.id)
    await message.answer(text, reply_markup=kb.driver_keyboard_for_state(state_code))


# =========================================================================
# /start
# =========================================================================
@driver_router.message(CommandStart(deep_link=True), StateFilter(any_state))
async def start_with_token(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    owner_bot: Bot,
) -> None:
    await state.clear()
    token = (command.args or "").strip()

    existing = await _driver_by_telegram(session, message.from_user.id)
    if existing is not None:
        await _refresh_ui(
            message, session, existing,
            msg.DRIVER_WELCOME_BACK.format(name=existing.full_name),
        )
        return

    driver = await _driver_by_invite(session, token)
    if driver is None or driver.telegram_id is not None:
        await message.answer(msg.DRIVER_INVITE_INVALID)
        return

    driver.telegram_id = message.from_user.id
    driver.invite_token = None
    await session.commit()

    await _refresh_ui(
        message, session, driver,
        msg.DRIVER_REGISTERED.format(name=driver.full_name),
    )

    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_DRIVER_REGISTERED.format(name=driver.full_name),
        )


@driver_router.message(CommandStart(), StateFilter(any_state))
async def start_no_token(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    await _refresh_ui(
        message, session, driver,
        msg.DRIVER_WELCOME_BACK.format(name=driver.full_name),
    )


# =========================================================================
# /help, /status, /cancel
# =========================================================================
@driver_router.message(Command("help"), StateFilter(any_state))
async def cmd_help(message: Message) -> None:
    await message.answer(msg.DRIVER_HELP)


@driver_router.message(Command("status"), StateFilter(any_state))
@driver_router.message(F.text == kb.BTN_STATUS, StateFilter(any_state))
async def cmd_status(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return

    owner = await session.get(Owner, driver.owner_id)
    tz_name = owner.timezone if owner else None
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        text = msg.STATUS_NO_SHIFT
    else:
        vehicle = await session.get(Vehicle, shift.vehicle_id)
        plate = vehicle.license_plate if vehicle else "—"
        trip = await trip_service.get_active_trip(session, shift.id)
        if trip is None:
            trips = await shift_service.get_shift_trips(session, shift.id)
            text = msg.STATUS_SHIFT_NO_TRIP.format(
                plate=plate,
                started=fmt_time(shift.started_at, tz_name),
                trips_count=len(trips),
            )
        else:
            status_label = {
                "created": "создан",
                "in_transit": "в пути",
                "unloading": "на выгрузке",
            }.get(trip.status, trip.status)
            text = msg.STATUS_TRIP.format(
                status=status_label,
                origin=trip.origin or "—",
                destination=trip.destination or "—",
                plate=plate,
            )

    await _refresh_ui(message, session, driver, text)


@driver_router.message(Command("cancel"), StateFilter(any_state))
async def cmd_cancel(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.CANCELLED)
        return
    await _refresh_ui(message, session, driver, msg.CANCELLED)


# /balance удалён по решению владельца: водитель не должен видеть свой
# заработок в боте — суммы объясняет сам владелец. Владельцу зарплата
# по-прежнему приходит в уведомлении о завершении смены и видна на сайте.


# =========================================================================
# НАЧАЛО СМЕНЫ
# =========================================================================
async def _do_start_shift(
    reply_target: Message,
    state: FSMContext,
    session: AsyncSession,
    owner_bot: Bot,
    driver: Driver,
    vehicle: Vehicle,
    odometer_start: int | None,
    photo_file_id: str | None,
    source_bot: Bot | None = None,
) -> None:
    """Создать смену и уведомить. Общий хвост для всех путей старта.
    Фото-режим (Правка 1): водитель прислал только фото — оно уходит владельцу
    с кнопкой «Указать пробег», число км вписывает владелец."""
    shift = await shift_service.start_shift(
        session,
        owner_id=driver.owner_id,
        driver_id=driver.id,
        vehicle_id=vehicle.id,
        odometer_start=odometer_start,
        photo_file_id=photo_file_id,
    )
    await session.flush()
    await log_event(
        session,
        owner_id=driver.owner_id,
        driver_id=driver.id,
        shift_id=shift.id,
        event_type="shift_started",
        payload={"vehicle_id": vehicle.id, "odometer_start": odometer_start},
    )
    await session.commit()
    await state.clear()

    if odometer_start is not None:
        driver_text = msg.SHIFT_STARTED.format(plate=vehicle.license_plate, km=odometer_start)
    else:
        driver_text = msg.SHIFT_STARTED_SIMPLE.format(plate=vehicle.license_plate)
    await _refresh_ui(reply_target, session, driver, driver_text)

    owner = await session.get(Owner, driver.owner_id)
    if owner is None:
        return
    # Фото-режим: шлём владельцу фото одометра + кнопку «Указать пробег».
    if photo_file_id and odometer_start is None and source_bot is not None:
        await transfer_photo_to_owner(
            source_bot=source_bot, owner_bot=owner_bot, session=session, owner=owner,
            source_file_id=photo_file_id,
            caption=msg.ODOMETER_PHOTO_TO_OWNER_START.format(
                driver=driver.full_name, plate=vehicle.license_plate
            ),
            reply_markup=kb.odometer_set_keyboard(shift.id, "start"),
        )
        return
    if odometer_start is not None:
        owner_text = msg.NOTIFY_SHIFT_STARTED.format(
            driver=driver.full_name, plate=vehicle.license_plate, km=odometer_start
        )
    else:
        owner_text = msg.NOTIFY_SHIFT_STARTED_SIMPLE.format(
            driver=driver.full_name, plate=vehicle.license_plate
        )
    await notify_owner(owner_bot, session, owner, owner_text)


@driver_router.message(F.text == kb.BTN_START_SHIFT, StateFilter(any_state))
async def btn_start_shift(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return

    if await shift_service.get_active_shift(session, driver.id) is not None:
        await _refresh_ui(message, session, driver, msg.SHIFT_ALREADY_OPEN)
        return

    vehicles = await shift_service.get_free_vehicles(session, driver.owner_id)
    if not vehicles:
        await _refresh_ui(message, session, driver, msg.SHIFT_NO_FREE_VEHICLES)
        return

    # Закреплённая машина: если у водителя есть «обычная машина» и она сейчас
    # свободна — стартуем на ней БЕЗ выбора (удобно, водитель не мискликнет).
    # Занята → мягко предупреждаем и показываем выбор свободных.
    if driver.default_vehicle_id is not None:
        default_veh = next(
            (v for v in vehicles if v.id == driver.default_vehicle_id), None
        )
        if default_veh is not None:
            await _begin_shift_on_default(
                message, state, session, owner_bot, driver, default_veh
            )
            return
        busy_default = await session.get(Vehicle, driver.default_vehicle_id)
        if busy_default is not None and busy_default.is_active:
            await message.answer(
                msg.SHIFT_DEFAULT_BUSY.format(plate=busy_default.license_plate)
            )
            await state.set_state(StartShift.selecting_vehicle)
            await message.answer(
                msg.SHIFT_PICK_VEHICLE, reply_markup=kb.vehicle_pick_keyboard(vehicles)
            )
            return

    # Упрощённый старт: без одометра и, если машина одна — без выбора,
    # сразу открываем смену (FEATURE_ODOMETER_PHOTO выключен).
    if not settings.feature_odometer_photo and len(vehicles) == 1:
        vehicle = vehicles[0]
        # Анти-миссклик: даже единственная свободная машина может оказаться
        # чужой (свою уже занял другой водитель) — переспросим.
        default_plate = await _unusual_vehicle_plate(session, driver, vehicle)
        if default_plate is not None:
            await state.set_state(StartShift.confirming_vehicle)
            await state.update_data(vehicle_id=vehicle.id, default_plate=default_plate)
            await message.answer(
                msg.SHIFT_CONFIRM_UNUSUAL_VEHICLE.format(
                    default_plate=default_plate, plate=vehicle.license_plate
                ),
                reply_markup=kb.vehicle_confirm_keyboard(),
            )
            return
        await _do_start_shift(
            message, state, session, owner_bot, driver, vehicle,
            odometer_start=None, photo_file_id=None,
        )
        return

    await state.set_state(StartShift.selecting_vehicle)
    await message.answer(msg.SHIFT_PICK_VEHICLE, reply_markup=kb.vehicle_pick_keyboard(vehicles))


@driver_router.callback_query(StartShift.selecting_vehicle, F.data == "shift:cancel")
async def cb_shift_cancel(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, call.from_user.id)
    if driver is not None:
        await call.message.delete()
        await _refresh_ui(call.message, session, driver, msg.CANCELLED)
    await call.answer()


async def _begin_shift_on_default(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    owner_bot: Bot,
    driver: Driver,
    vehicle: Vehicle,
) -> None:
    """Старт смены на закреплённой машине без выбора. При фото-режиме одометра
    даём кнопку «🔁 Другая машина» — вдруг сегодня водитель на другой."""
    if not settings.feature_odometer_photo:
        await _do_start_shift(
            message, state, session, owner_bot, driver, vehicle,
            odometer_start=None, photo_file_id=None,
        )
        return
    # Машина привязана — никакого выбора/кнопок, только просьба фото одометра.
    await state.update_data(vehicle_id=vehicle.id)
    await state.set_state(StartShift.waiting_for_odometer_photo)
    await message.answer(
        msg.SHIFT_ODOMETER_ON_DEFAULT.format(plate=vehicle.license_plate)
    )


async def _unusual_vehicle_plate(
    session: AsyncSession, driver: Driver, vehicle: Vehicle
) -> str | None:
    """Гос.номер «обычной» машины водителя, если он берёт ДРУГУЮ.
    None — выбор обычный (или «обычная машина» не назначена)."""
    if driver.default_vehicle_id is None or driver.default_vehicle_id == vehicle.id:
        return None
    default_vehicle = await session.get(Vehicle, driver.default_vehicle_id)
    if default_vehicle is None or not default_vehicle.is_active:
        return None
    return default_vehicle.license_plate


async def _continue_shift_start(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    owner_bot: Bot,
    driver: Driver,
    vehicle: Vehicle,
) -> None:
    """Общее продолжение после выбора (и, если надо, подтверждения) машины."""
    # Без одометра — открываем смену сразу после выбора машины.
    if not settings.feature_odometer_photo:
        await call.message.edit_text(f"Машина: <b>{vehicle.license_plate}</b>")
        await call.answer()
        await _do_start_shift(
            call.message, state, session, owner_bot, driver, vehicle,
            odometer_start=None, photo_file_id=None,
        )
        return

    await state.update_data(vehicle_id=vehicle.id)
    await state.set_state(StartShift.waiting_for_odometer_photo)
    await call.message.edit_text(f"Машина: <b>{vehicle.license_plate}</b>")
    await call.message.answer(msg.SHIFT_ASK_ODOMETER_PHOTO_START + " 📷")
    await call.answer()


@driver_router.callback_query(StartShift.selecting_vehicle, F.data.startswith("shift:pick:"))
async def cb_shift_pick_vehicle(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    vehicle_id = int(call.data.split(":")[2])
    vehicle = await session.get(Vehicle, vehicle_id)
    driver = await _driver_by_telegram(session, call.from_user.id)
    if vehicle is None or driver is None or vehicle.owner_id != driver.owner_id:
        await call.answer("Машина недоступна", show_alert=True)
        return

    # Анти-миссклик: выбрана не «обычная» машина — мягко переспросим.
    default_plate = await _unusual_vehicle_plate(session, driver, vehicle)
    if default_plate is not None:
        await state.set_state(StartShift.confirming_vehicle)
        await state.update_data(vehicle_id=vehicle.id, default_plate=default_plate)
        await call.message.edit_text(
            msg.SHIFT_CONFIRM_UNUSUAL_VEHICLE.format(
                default_plate=default_plate, plate=vehicle.license_plate
            ),
            reply_markup=kb.vehicle_confirm_keyboard(),
        )
        await call.answer()
        return

    await _continue_shift_start(call, state, session, owner_bot, driver, vehicle)


@driver_router.callback_query(StartShift.confirming_vehicle, F.data == "shift:pickok")
async def cb_shift_confirm_vehicle(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    """Водитель подтвердил, что осознанно берёт не свою обычную машину."""
    driver = await _driver_by_telegram(session, call.from_user.id)
    data = await state.get_data()
    vehicle = await session.get(Vehicle, data.get("vehicle_id") or 0)
    if driver is None or vehicle is None or vehicle.owner_id != driver.owner_id:
        await state.clear()
        await call.answer(msg.SOMETHING_WRONG, show_alert=True)
        return

    # Пока водитель думал, машину мог занять другой — перепроверяем.
    free = await shift_service.get_free_vehicles(session, driver.owner_id)
    if vehicle.id not in {v.id for v in free}:
        if not free:
            await state.clear()
            await call.message.edit_text(msg.SHIFT_NO_FREE_VEHICLES)
        else:
            await state.set_state(StartShift.selecting_vehicle)
            await call.message.edit_text(
                "Эту машину уже заняли. " + msg.SHIFT_PICK_VEHICLE,
                reply_markup=kb.vehicle_pick_keyboard(free),
            )
        await call.answer()
        return

    # Владельцу — сигнал, что водитель сел не в свою обычную машину.
    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_UNUSUAL_VEHICLE.format(
                driver=driver.full_name,
                plate=vehicle.license_plate,
                default_plate=data.get("default_plate") or "—",
            ),
        )
    await _continue_shift_start(call, state, session, owner_bot, driver, vehicle)


@driver_router.callback_query(StartShift.confirming_vehicle, F.data == "shift:repick")
async def cb_shift_repick_vehicle(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """«Выбрать заново» — вернуться к списку свободных машин."""
    driver = await _driver_by_telegram(session, call.from_user.id)
    if driver is None:
        await state.clear()
        await call.answer(msg.SOMETHING_WRONG, show_alert=True)
        return
    free = await shift_service.get_free_vehicles(session, driver.owner_id)
    if not free:
        await state.clear()
        await call.message.edit_text(msg.SHIFT_NO_FREE_VEHICLES)
        await call.answer()
        return
    await state.set_state(StartShift.selecting_vehicle)
    await call.message.edit_text(
        msg.SHIFT_PICK_VEHICLE, reply_markup=kb.vehicle_pick_keyboard(free)
    )
    await call.answer()


@driver_router.message(StartShift.waiting_for_odometer_photo, F.photo)
async def shift_start_odometer_photo(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot, bot: Bot
) -> None:
    # Водитель шлёт ТОЛЬКО фото (Правка 1) — число км впишет владелец.
    file_id = _pick_photo_file_id(message)
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return
    data = await state.get_data()
    vehicle = await session.get(Vehicle, data.get("vehicle_id"))
    if vehicle is None:
        await state.clear()
        await _refresh_ui(message, session, driver, msg.SOMETHING_WRONG)
        return
    await _do_start_shift(
        message, state, session, owner_bot, driver, vehicle,
        odometer_start=None, photo_file_id=file_id, source_bot=bot,
    )


@driver_router.message(StartShift.waiting_for_odometer_photo)
async def shift_start_odometer_photo_invalid(message: Message) -> None:
    await message.answer(msg.SHIFT_ASK_ODOMETER_PHOTO_START + " 📷")


# =========================================================================
# ЗАВЕРШЕНИЕ СМЕНЫ — с контролем топлива
# =========================================================================
@driver_router.message(F.text == kb.BTN_END_SHIFT, StateFilter(any_state))
async def btn_end_shift(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        await _refresh_ui(message, session, driver, msg.SHIFT_NO_ACTIVE)
        return
    if await trip_service.get_active_trip(session, shift.id) is not None:
        await _refresh_ui(message, session, driver, msg.SHIFT_TRIP_OPEN_CANT_END)
        return

    # Без одометра — завершаем смену сразу (FEATURE_ODOMETER_PHOTO выключен).
    if not settings.feature_odometer_photo:
        await _do_end_shift(message, state, session, owner_bot, driver, shift, odometer_end=None)
        return

    await state.set_state(EndShift.waiting_for_odometer_photo)
    await message.answer(msg.SHIFT_ASK_ODOMETER_PHOTO_END + " 📷")


@driver_router.message(EndShift.waiting_for_odometer_photo, F.photo)
async def shift_end_odometer_photo(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot, bot: Bot
) -> None:
    # Только фото (Правка 1): число км в конце смены впишет владелец.
    file_id = _pick_photo_file_id(message)
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        await state.clear()
        await _refresh_ui(message, session, driver, msg.SHIFT_NO_ACTIVE)
        return
    await state.update_data(odometer_photo=file_id)
    await _do_end_shift(
        message, state, session, owner_bot, driver, shift,
        odometer_end=None, source_bot=bot,
    )


@driver_router.message(EndShift.waiting_for_odometer_photo)
async def shift_end_odometer_photo_invalid(message: Message) -> None:
    await message.answer(msg.SHIFT_ASK_ODOMETER_PHOTO_END + " 📷")


async def _do_end_shift(
    reply_target: Message,
    state: FSMContext,
    session: AsyncSession,
    owner_bot: Bot,
    driver: Driver,
    shift: Shift,
    odometer_end: int | None,
    source_bot: Bot | None = None,
) -> None:
    """Завершить смену и уведомить. Общий хвост для путей с одометром и без.
    Зарплату водителю показываем только при FEATURE_SHOW_SALARY."""
    data = await state.get_data()
    end_photo = data.get("odometer_photo")
    await shift_service.end_shift(
        session,
        shift=shift,
        odometer_end=odometer_end,
        photo_file_id=data.get("odometer_photo"),
        ended_at=datetime.now(timezone.utc),
    )
    await session.flush()
    await session.refresh(shift)

    trips = await shift_service.get_shift_trips(session, shift.id)
    revenue = sum((t.revenue_rub or Decimal(0)) for t in trips) or Decimal(0)
    pending_revenue = sum((t.driver_revenue_pending_rub or Decimal(0)) for t in trips) or Decimal(0)

    approved_expenses = await session.execute(
        select(Expense).where(Expense.shift_id == shift.id, Expense.status == "approved")
    )
    approved_list = list(approved_expenses.scalars().all())
    expenses_total = sum((e.amount_rub or Decimal(0)) for e in approved_list) or Decimal(0)

    salary = salary_service.calculate_salary(driver, shift, trips)

    await log_event(
        session,
        owner_id=driver.owner_id,
        driver_id=driver.id,
        shift_id=shift.id,
        event_type="shift_completed",
        payload={"distance_km": shift.distance_km, "trips": len(trips), "salary": str(salary)},
    )
    await session.commit()
    await state.clear()

    # Водителю зарплату НЕ показываем (решение владельца): только факт
    # завершения. Сумма уходит владельцу в уведомлении ниже.
    driver_text = msg.SHIFT_COMPLETED_DRIVER_SIMPLE.format(trips=len(trips))
    await _refresh_ui(reply_target, session, driver, driver_text)

    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        # В фото-режиме пробег/зарплата ещё не известны — не показываем нули.
        if odometer_end is None:
            owner_text = msg.NOTIFY_SHIFT_COMPLETED_PENDING.format(
                driver=driver.full_name, trips=len(trips),
                revenue=f"{revenue:.0f}", expenses=f"{expenses_total:.0f}",
            )
        else:
            owner_text = msg.NOTIFY_SHIFT_COMPLETED.format(
                driver=driver.full_name, distance=shift.distance_km or 0,
                trips=len(trips), revenue=f"{revenue:.0f}",
                expenses=f"{expenses_total:.0f}", salary=f"{salary:.0f}",
            )
        if pending_revenue:
            owner_text += f"\n⏳ Выручка на подтверждении: <b>{pending_revenue:.0f} ₽</b>"
        await notify_owner(owner_bot, session, owner, owner_text)
        # Контроль топлива: сумма fuel-расходов смены vs норма
        await _maybe_fuel_overrun_alert(session, owner_bot, owner, driver, shift, approved_list)
        # Фото-режим: фото одометра конца смены → владельцу с кнопкой «Указать пробег».
        if odometer_end is None and end_photo and source_bot is not None:
            vehicle = await session.get(Vehicle, shift.vehicle_id)
            await transfer_photo_to_owner(
                source_bot=source_bot, owner_bot=owner_bot, session=session, owner=owner,
                source_file_id=end_photo,
                caption=msg.ODOMETER_PHOTO_TO_OWNER_END.format(
                    driver=driver.full_name,
                    plate=vehicle.license_plate if vehicle else "—",
                ),
                reply_markup=kb.odometer_set_keyboard(shift.id, "end"),
            )

    # Где закончил смену: просим геопозицию (скип возможен) — владельцу
    # уйдёт точка ссылкой на Яндекс.Карты.
    if settings.feature_cargo_geolocation:
        await state.set_state(EndShiftLocation.waiting_for_location)
        await state.update_data(ended_shift_id=shift.id)
        await reply_target.answer(
            msg.SHIFT_END_LOCATION_ASK, reply_markup=kb.location_request_keyboard()
        )


@driver_router.message(EndShiftLocation.waiting_for_location, F.location)
async def shift_end_with_location(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return
    data = await state.get_data()
    lat, lon = message.location.latitude, message.location.longitude
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=data.get("ended_shift_id"), event_type="location_sent",
        payload={"lat": lat, "lon": lon, "context": "shift_end"},
    )
    await session.commit()
    await state.clear()
    await _refresh_ui(message, session, driver, msg.LOCATION_SAVED)

    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        # Формат ссылки Яндекс.Карт: pt=долгота,широта
        link = f"https://yandex.ru/maps/?pt={lon},{lat}&z=16&l=map"
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_SHIFT_END_LOCATION.format(driver=driver.full_name, link=link),
        )


@driver_router.message(EndShiftLocation.waiting_for_location, F.text == kb.BTN_SKIP)
async def shift_end_skip_location(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    await state.clear()
    if driver is not None:
        await _refresh_ui(message, session, driver, "Ок, без геопозиции.")


@driver_router.message(EndShiftLocation.waiting_for_location)
async def shift_end_location_invalid(message: Message) -> None:
    await message.answer(msg.SHIFT_END_LOCATION_ASK)


async def _maybe_fuel_overrun_alert(
    session: AsyncSession,
    owner_bot: Bot,
    owner: Owner,
    driver: Driver,
    shift: Shift,
    approved_expenses: list[Expense],
) -> None:
    """Если фактический расход >10% выше нормы машины — уведомить владельца."""
    vehicle = await session.get(Vehicle, shift.vehicle_id)
    if vehicle is None or vehicle.fuel_norm_per_100km is None:
        return
    if not shift.distance_km or shift.distance_km <= 0:
        return
    fuel_rub = sum(
        (e.amount_rub or Decimal(0)) for e in approved_expenses if e.category == "fuel"
    )
    if fuel_rub <= 0:
        return
    liters = trip_service.liters_from_rub(Decimal(fuel_rub))
    actual_per_100 = (liters / Decimal(shift.distance_km)) * Decimal(100)
    norm = Decimal(vehicle.fuel_norm_per_100km)
    if actual_per_100 <= norm * Decimal("1.10"):
        return
    pct = ((actual_per_100 / norm) - Decimal(1)) * Decimal(100)
    # сколько примерно «лишних» рублей потрачено на топливо
    expected_liters = (norm * Decimal(shift.distance_km) / Decimal(100))
    excess_liters = liters - expected_liters
    excess_rub = (excess_liters * trip_service.DEFAULT_FUEL_PRICE_RUB_PER_LITER).quantize(Decimal("0.01"))
    await log_event(
        session, owner_id=owner.id, driver_id=driver.id, shift_id=shift.id,
        event_type="fuel_overrun_alert",
        payload={"percent": float(pct), "excess_rub": str(excess_rub)},
    )
    await session.commit()
    await notify_owner(
        owner_bot, session, owner,
        msg.ALERT_FUEL_OVERRUN.format(
            driver=driver.full_name, plate=vehicle.license_plate,
            percent=f"{pct:.0f}", actual=float(actual_per_100), norm=norm,
        ),
    )


# =========================================================================
# НОВЫЙ РЕЙС (с шаблонами маршрутов)
# =========================================================================
@driver_router.message(F.text == kb.BTN_NEW_TRIP, StateFilter(any_state))
async def btn_new_trip(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        await _refresh_ui(message, session, driver, msg.TRIP_NEED_SHIFT)
        return
    if await trip_service.get_active_trip(session, shift.id) is not None:
        await _refresh_ui(message, session, driver, msg.TRIP_ALREADY_OPEN)
        return

    # есть ли у владельца шаблоны? Если да — предложим выбрать
    templates_res = await session.execute(
        select(RouteTemplate)
        .where(RouteTemplate.owner_id == driver.owner_id, RouteTemplate.is_active.is_(True))
        .order_by(RouteTemplate.name)
    )
    templates = list(templates_res.scalars().all())
    if templates:
        await state.set_state(NewTrip.waiting_for_origin)  # помечаем стартом flow
        await state.update_data(picking_template=True)
        await message.answer(
            "Выберите маршрут из шаблона или введите свой:",
            reply_markup=kb.route_template_keyboard(templates),
        )
        return

    await state.set_state(NewTrip.waiting_for_origin)
    await message.answer(msg.TRIP_ASK_ORIGIN)


@driver_router.callback_query(F.data.startswith("rt:pick:"))
async def cb_route_template_pick(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    template_id = int(call.data.split(":")[2])
    template = await session.get(RouteTemplate, template_id)
    driver = await _driver_by_telegram(session, call.from_user.id)
    if driver is None or template is None or template.owner_id != driver.owner_id:
        await call.answer("Шаблон недоступен", show_alert=True)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        await state.clear()
        await call.answer("Сначала откройте смену", show_alert=True)
        return

    # Не создаём сразу — сперва даём подтвердить/переснять (баг E3).
    await state.update_data(
        origin=template.origin,
        destination=template.destination,
        cargo=template.default_cargo or template.name,
    )
    await call.message.edit_text(
        f"Маршрут: <b>{template.origin} → {template.destination}</b>",
        reply_markup=kb.route_confirm_keyboard(),
    )
    await call.answer()


@driver_router.callback_query(F.data == "rt:confirm")
async def cb_route_confirm(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    driver = await _driver_by_telegram(session, call.from_user.id)
    if driver is None:
        await call.answer(msg.DRIVER_LINK_EXPECTED, show_alert=True)
        return
    data = await state.get_data()
    origin, destination, cargo = data.get("origin"), data.get("destination"), data.get("cargo")
    if not origin or not destination:
        await call.answer("Маршрут не выбран", show_alert=True)
        return
    await call.message.delete()
    await _finalize_new_trip(
        call.message, state, session, owner_bot, driver,
        origin=origin, destination=destination, cargo=cargo,
    )
    await call.answer()


@driver_router.callback_query(F.data == "rt:change")
async def cb_route_change(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """Переснять выбор маршрута — снова показать шаблоны (баг E3)."""
    driver = await _driver_by_telegram(session, call.from_user.id)
    if driver is None:
        await call.answer(msg.DRIVER_LINK_EXPECTED, show_alert=True)
        return
    templates_res = await session.execute(
        select(RouteTemplate)
        .where(RouteTemplate.owner_id == driver.owner_id, RouteTemplate.is_active.is_(True))
        .order_by(RouteTemplate.name)
    )
    templates = list(templates_res.scalars().all())
    await state.set_state(NewTrip.waiting_for_origin)
    await state.update_data(picking_template=True)
    if templates:
        await call.message.edit_text(
            "Выберите маршрут из шаблона или введите свой:",
            reply_markup=kb.route_template_keyboard(templates),
        )
    else:
        await call.message.edit_text(msg.TRIP_ASK_ORIGIN)
    await call.answer()


@driver_router.callback_query(F.data == "rt:manual")
async def cb_route_manual(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NewTrip.waiting_for_origin)
    await state.update_data(picking_template=False)
    await call.message.delete()
    await call.message.answer(msg.TRIP_ASK_ORIGIN)
    await call.answer()


@driver_router.callback_query(F.data == "rt:cancel")
async def cb_route_cancel(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, call.from_user.id)
    await call.message.delete()
    if driver is not None:
        await _refresh_ui(call.message, session, driver, msg.CANCELLED)
    await call.answer()


@driver_router.message(NewTrip.waiting_for_origin)
async def trip_origin(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("picking_template"):
        # пользователь напечатал текст вместо выбора шаблона — переключаемся в ручной
        await state.update_data(picking_template=False)
    text = (message.text or "").strip()
    if not text:
        await message.answer(msg.TRIP_ASK_ORIGIN)
        return
    await state.update_data(origin=text)
    await state.set_state(NewTrip.waiting_for_destination)
    await message.answer(msg.TRIP_ASK_DESTINATION)


@driver_router.message(NewTrip.waiting_for_destination)
async def trip_destination(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(msg.TRIP_ASK_DESTINATION)
        return
    await state.update_data(destination=text)

    # Без вопроса «что везёте» (FEATURE_TRIP_CARGO выключен) — создаём рейс сразу.
    if not settings.feature_trip_cargo:
        driver = await _driver_by_telegram(session, message.from_user.id)
        if driver is None:
            await state.clear()
            await message.answer(msg.SOMETHING_WRONG)
            return
        data = await state.get_data()
        await _finalize_new_trip(
            message, state, session, owner_bot, driver,
            origin=data["origin"], destination=text, cargo=None,
        )
        return

    await state.set_state(NewTrip.waiting_for_cargo)
    await message.answer(msg.TRIP_ASK_CARGO)


async def _finalize_new_trip(
    reply_target: Message,
    state: FSMContext,
    session: AsyncSession,
    owner_bot: Bot,
    driver: Driver,
    origin: str,
    destination: str,
    cargo: str | None,
) -> None:
    """Создать рейс и уведомить. Общий хвост для путей с грузом и без."""
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        await state.clear()
        await _refresh_ui(reply_target, session, driver, msg.TRIP_NEED_SHIFT)
        return
    trip = await trip_service.create_trip(
        session, shift=shift, origin=origin, destination=destination, cargo_name=cargo,
    )
    await session.flush()
    await log_event(
        session,
        owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_created",
        payload={"origin": origin, "destination": destination},
    )
    await session.commit()
    await state.clear()

    cargo_display = cargo or "—"
    await _refresh_ui(
        reply_target, session, driver,
        msg.TRIP_CREATED.format(origin=origin, destination=destination, cargo=cargo_display),
    )
    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_TRIP_CREATED.format(
                driver=driver.full_name, origin=origin,
                destination=destination, cargo=cargo_display,
            ),
        )


@driver_router.message(NewTrip.waiting_for_cargo)
async def trip_cargo(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    cargo = (message.text or "").strip()
    if not cargo:
        await message.answer(msg.TRIP_ASK_CARGO)
        return

    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return

    data = await state.get_data()
    await _finalize_new_trip(
        message, state, session, owner_bot, driver,
        origin=data["origin"], destination=data["destination"], cargo=cargo,
    )


# =========================================================================
# ВЫЕХАЛ (created → in_transit) — с геопозицией (Правка 2)
# =========================================================================
@driver_router.message(F.text == kb.BTN_TRIP_DEPART, StateFilter(any_state))
async def btn_trip_depart(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        await _refresh_ui(message, session, driver, msg.SHIFT_NO_ACTIVE)
        return
    trip = await trip_service.get_active_trip(session, shift.id)
    if trip is None or trip.status != "created":
        await _refresh_ui(message, session, driver, msg.TRIP_WRONG_STATUS)
        return

    # Просим геопозицию при выезде (скип возможен). Иначе — выезжаем сразу.
    if not settings.feature_cargo_geolocation:
        await _do_depart(message, state, session, owner_bot, location=None)
        return
    await state.set_state(TripDepartLocation.waiting_for_location)
    await message.answer(msg.LOCATION_ASK, reply_markup=kb.location_request_keyboard())


@driver_router.message(TripDepartLocation.waiting_for_location, F.location)
async def depart_with_location(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    await _do_depart(
        message, state, session, owner_bot,
        location=(message.location.latitude, message.location.longitude),
    )


@driver_router.message(TripDepartLocation.waiting_for_location, F.text == kb.BTN_SKIP)
async def depart_skip_location(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    await _do_depart(message, state, session, owner_bot, location=None)


@driver_router.message(TripDepartLocation.waiting_for_location)
async def depart_location_invalid(message: Message) -> None:
    await message.answer(msg.LOCATION_ASK)


async def _do_depart(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    owner_bot: Bot,
    location: tuple[float, float] | None,
) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    trip = await trip_service.get_active_trip(session, shift.id) if shift else None
    if trip is None or trip.status != "created":
        await state.clear()
        await _refresh_ui(message, session, driver, msg.TRIP_WRONG_STATUS)
        return

    await trip_service.set_trip_status(session, trip=trip, status="in_transit")
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_in_transit",
    )
    if location is not None:
        await log_event(
            session, owner_id=driver.owner_id, driver_id=driver.id,
            shift_id=shift.id, trip_id=trip.id, event_type="location_sent",
            payload={"lat": location[0], "lon": location[1], "context": "depart"},
        )
    await session.commit()
    await state.clear()
    await _refresh_ui(message, session, driver, msg.TRIP_IN_TRANSIT_DRIVER)
    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_TRIP_IN_TRANSIT.format(
                driver=driver.full_name, origin=trip.origin or "—",
                destination=trip.destination or "—",
            ),
        )


# =========================================================================
# ВЫГРУЗКА (in_transit → unloading) — с геопозицией
# =========================================================================
def _can_end_trip_status(status: str) -> bool:
    """Из каких статусов разрешено «Сдал груз». При выключенных промежуточных
    статусах (FEATURE_TRIP_STATUS_STEPS) рейс завершается прямо из in_transit."""
    if settings.feature_trip_status_steps:
        return status == "unloading"
    return status in ("in_transit", "unloading")


@driver_router.message(F.text == kb.BTN_TRIP_UNLOADING, StateFilter(any_state))
async def btn_trip_unloading_start(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    trip = await trip_service.get_active_trip(session, shift.id) if shift else None
    if trip is None or trip.status != "in_transit":
        await _refresh_ui(message, session, driver, msg.TRIP_WRONG_STATUS)
        return

    # Без геопозиции при сдаче (FEATURE_CARGO_GEOLOCATION выключен) — сразу.
    if not settings.feature_cargo_geolocation:
        await _do_unloading(message, state, session, owner_bot, location=None)
        return

    await state.set_state(UnloadingLocation.waiting_for_location)
    await message.answer(msg.LOCATION_ASK, reply_markup=kb.location_request_keyboard())


@driver_router.message(UnloadingLocation.waiting_for_location, F.location)
async def unloading_with_location(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    await _do_unloading(
        message, state, session, owner_bot,
        location=(message.location.latitude, message.location.longitude),
    )


@driver_router.message(UnloadingLocation.waiting_for_location, F.text == kb.BTN_SKIP)
async def unloading_skip_location(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    await _do_unloading(message, state, session, owner_bot, location=None)


@driver_router.message(UnloadingLocation.waiting_for_location)
async def unloading_location_invalid(message: Message) -> None:
    await message.answer(msg.LOCATION_ASK)


async def _do_unloading(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    owner_bot: Bot,
    location: tuple[float, float] | None,
) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    trip = await trip_service.get_active_trip(session, shift.id) if shift else None
    if trip is None or trip.status != "in_transit":
        await state.clear()
        await _refresh_ui(message, session, driver, msg.TRIP_WRONG_STATUS)
        return

    await trip_service.set_trip_status(session, trip=trip, status="unloading")
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_unloading",
    )
    if location is not None:
        await log_event(
            session, owner_id=driver.owner_id, driver_id=driver.id,
            shift_id=shift.id, trip_id=trip.id, event_type="location_sent",
            payload={"lat": location[0], "lon": location[1], "context": "unloading"},
        )
    await session.commit()
    await state.clear()

    await _refresh_ui(message, session, driver, msg.TRIP_UNLOADING_DRIVER)
    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_TRIP_UNLOADING.format(
                driver=driver.full_name, destination=trip.destination or "—",
            ),
        )


# =========================================================================
# ЗАГРУЗИТЬ ТТН
# =========================================================================
@driver_router.message(F.text == kb.BTN_UPLOAD_WAYBILL, StateFilter(any_state))
async def btn_upload_waybill(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        await _refresh_ui(message, session, driver, msg.SHIFT_NO_ACTIVE)
        return
    trip = await trip_service.get_active_trip(session, shift.id)
    if trip is None:
        await _refresh_ui(message, session, driver, msg.TRIP_WRONG_STATUS)
        return

    await state.set_state(UploadWaybill.waiting_for_photo)
    await message.answer(
        "Сфотографируйте документ 📷\n"
        "Не нужен — просто нажмите любую кнопку внизу (шаг необязательный)."
    )


@driver_router.message(UploadWaybill.waiting_for_photo, F.photo)
async def upload_waybill_photo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    owner_bot: Bot,
) -> None:
    file_id = _pick_photo_file_id(message)
    if file_id is None:
        await message.answer(msg.WAYBILL_PHOTO_REQUIRED)
        return

    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    trip = await trip_service.get_active_trip(session, shift.id) if shift else None
    if trip is None:
        await state.clear()
        await _refresh_ui(message, session, driver, msg.TRIP_NO_ACTIVE)
        return

    await trip_service.attach_waybill(session, trip=trip, photo_file_id=file_id)
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="waybill_uploaded",
    )
    await session.commit()
    await state.clear()

    await _refresh_ui(message, session, driver, msg.WAYBILL_SAVED)

    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        caption = msg.NOTIFY_WAYBILL.format(
            driver=driver.full_name,
            origin=trip.origin or "—", destination=trip.destination or "—",
        )
        await transfer_photo_to_owner(
            source_bot=bot, owner_bot=owner_bot,
            session=session, owner=owner,
            source_file_id=file_id, caption=caption,
        )


@driver_router.message(
    UploadWaybill.waiting_for_photo, ~F.text.in_(kb.ALL_DRIVER_BUTTONS)
)
async def upload_waybill_invalid(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    """Не фото и не кнопка меню (случайный текст) — шаг необязательный, мягко
    выходим, чтобы водитель не залипал на «Нужно фото». Нажатия кнопок меню
    сюда НЕ попадают (исключены фильтром) — они уходят в свои обработчики,
    очищая это состояние сами."""
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is not None:
        await _refresh_ui(message, session, driver, "Ок, без документа — продолжаем.")
    else:
        await message.answer("Ок, без документа.")


# =========================================================================
# СДАЛ ГРУЗ (unloading → completed) — с геопозицией, без выручки/литров
# =========================================================================
@driver_router.message(F.text == kb.BTN_END_TRIP, StateFilter(any_state))
async def btn_end_trip_start(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    trip = await trip_service.get_active_trip(session, shift.id) if shift else None
    if trip is None or not _can_end_trip_status(trip.status):
        await _refresh_ui(message, session, driver, msg.TRIP_WRONG_STATUS)
        return

    # Без геопозиции при сдаче (FEATURE_CARGO_GEOLOCATION выключен) — сразу.
    if not settings.feature_cargo_geolocation:
        await _do_end_trip(message, state, session, owner_bot, location=None)
        return

    await state.set_state(EndTripLocation.waiting_for_location)
    await message.answer(msg.LOCATION_ASK, reply_markup=kb.location_request_keyboard())


@driver_router.message(EndTripLocation.waiting_for_location, F.location)
async def end_trip_with_location(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    await _do_end_trip(
        message, state, session, owner_bot,
        location=(message.location.latitude, message.location.longitude),
    )


@driver_router.message(EndTripLocation.waiting_for_location, F.text == kb.BTN_SKIP)
async def end_trip_skip_location(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    await _do_end_trip(message, state, session, owner_bot, location=None)


@driver_router.message(EndTripLocation.waiting_for_location)
async def end_trip_location_invalid(message: Message) -> None:
    await message.answer(msg.LOCATION_ASK)


async def _do_end_trip(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    owner_bot: Bot,
    location: tuple[float, float] | None,
) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    trip = await trip_service.get_active_trip(session, shift.id) if shift else None
    if trip is None or not _can_end_trip_status(trip.status):
        await state.clear()
        await _refresh_ui(message, session, driver, msg.TRIP_WRONG_STATUS)
        return

    await trip_service.complete_trip(session, trip=trip)
    await session.flush()
    await session.refresh(trip)
    fuel = Decimal(trip.fuel_cost_rub or 0)
    liters = trip_service.liters_from_rub(fuel) if fuel else Decimal(0)

    if location is not None:
        await log_event(
            session, owner_id=driver.owner_id, driver_id=driver.id,
            shift_id=shift.id, trip_id=trip.id, event_type="location_sent",
            payload={"lat": location[0], "lon": location[1], "context": "trip_end"},
        )
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_completed",
        payload={"fuel_cost": str(fuel)},
    )
    await session.commit()
    await state.clear()

    await _refresh_ui(
        message, session, driver,
        msg.TRIP_COMPLETED_DRIVER.format(fuel_cost=f"{fuel:.0f}", liters=float(liters)),
    )
    # Водитель отдал груз — он лучше знает сумму. Даём ему по желанию указать
    # выручку; владельцу придёт «Одобрить/Изменить» (выручка — одно поле,
    # владелец всегда главный, двойного счёта нет).
    await message.answer(
        msg.TRIP_DRIVER_REVENUE_ASK, reply_markup=kb.driver_revenue_keyboard(trip.id)
    )

    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_TRIP_COMPLETED.format(
                driver=driver.full_name,
                origin=trip.origin or "—", destination=trip.destination or "—",
                fuel=f"{fuel:.0f}",
            ),
            reply_markup=kb.trip_revenue_keyboard(trip.id),
        )


# =========================================================================
# ВОДИТЕЛЬ указывает выручку рейса (по желанию) → владельцу на подтверждение
# =========================================================================
@driver_router.callback_query(F.data.startswith("drev:"))
async def cb_driver_revenue(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    try:
        trip_id = int(call.data.split(":")[1])
    except (IndexError, ValueError):
        await call.answer("Некорректный запрос", show_alert=True)
        return
    driver = await _driver_by_telegram(session, call.from_user.id)
    trip = await session.get(Trip, trip_id)
    if driver is None or trip is None or trip.driver_id != driver.id:
        await call.answer("Рейс не найден", show_alert=True)
        return
    # Правило первого: если выручка уже указана — водитель её НЕ перетирает
    # (менять может только владелец). Убираем кнопку и выходим.
    if trip.revenue_rub is not None:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await call.answer(f"Выручка уже указана: {trip.revenue_rub:.0f} ₽", show_alert=True)
        return
    if trip.driver_revenue_pending_rub is not None:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        await call.answer("Выручка уже отправлена владельцу на подтверждение", show_alert=True)
        return
    # убрать кнопку, чтобы не нажимали повторно
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await state.set_state(DriverTripRevenue.waiting_for_amount)
    await state.update_data(trip_id=trip_id)
    await call.message.answer(
        msg.TRIP_DRIVER_REVENUE_ENTER.format(
            origin=trip.origin or "—", destination=trip.destination or "—"
        )
    )
    await call.answer()


@driver_router.message(DriverTripRevenue.waiting_for_amount)
async def driver_revenue_amount(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    amount = _parse_decimal(message.text)
    if amount is None or amount < 0:
        await message.answer(msg.TRIP_AMOUNT_INVALID)
        return
    data = await state.get_data()
    driver = await _driver_by_telegram(session, message.from_user.id)
    trip = await session.get(Trip, data.get("trip_id"))
    if driver is None or trip is None or trip.driver_id != driver.id:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return
    # Водительская сумма — только черновик на подтверждении. Финальной
    # выручкой она станет после решения владельца.
    saved = await trip_service.set_trip_driver_revenue_pending(
        session, trip=trip, revenue_rub=amount
    )
    if not saved:
        await session.commit()
        await state.clear()
        if trip.revenue_rub is not None:
            text = (
                f"Выручка уже указана владельцем: {trip.revenue_rub:.0f} ₽. "
                "Твоё число не записано."
            )
        elif trip.driver_revenue_pending_rub is not None:
            text = "Выручка уже отправлена владельцу на подтверждение. Второе число не записано."
        else:
            text = "Не получилось записать выручку. Попробуйте ещё раз."
        await _refresh_ui(message, session, driver, text)
        return
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=trip.shift_id, trip_id=trip.id,
        event_type="trip_revenue_from_driver", payload={"revenue": str(amount)},
    )
    await session.commit()
    await state.clear()
    await _refresh_ui(
        message, session, driver, msg.TRIP_DRIVER_REVENUE_DONE.format(amount=f"{amount:.0f}")
    )
    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_TRIP_REVENUE_FROM_DRIVER.format(
                driver=driver.full_name, origin=trip.origin or "—",
                destination=trip.destination or "—", amount=f"{amount:.0f}",
            ),
            reply_markup=kb.trip_revenue_decision_keyboard(trip.id),
        )


# =========================================================================
# РАСХОДЫ (для fuel показываем литры)
# =========================================================================
@driver_router.message(F.text == kb.BTN_EXPENSE, StateFilter(any_state))
async def btn_expense(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    # Расход можно вносить в любой момент — смена НЕ обязательна (Правка 3).
    await state.set_state(NewExpense.selecting_category)
    await message.answer(msg.EXPENSE_PICK_CATEGORY, reply_markup=kb.expense_category_keyboard())


@driver_router.callback_query(NewExpense.selecting_category, F.data == "exp_cat:cancel")
async def cb_expense_cancel(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, call.from_user.id)
    if driver is not None:
        await call.message.delete()
        await _refresh_ui(call.message, session, driver, msg.EXPENSE_CANCELLED)
    await call.answer()


@driver_router.callback_query(NewExpense.selecting_category, F.data.startswith("exp_cat:"))
async def cb_expense_category(call: CallbackQuery, state: FSMContext) -> None:
    category = call.data.split(":", 1)[1]
    if category not in expense_service.VALID_CATEGORIES:
        await call.answer("Неизвестная категория", show_alert=True)
        return
    await state.update_data(category=category)
    await state.set_state(NewExpense.waiting_for_amount)
    await call.message.edit_text(
        f"Категория: <b>{expense_service.CATEGORY_LABELS[category]}</b>"
    )
    await call.message.answer(msg.EXPENSE_ASK_AMOUNT)
    await call.answer()


@driver_router.message(NewExpense.waiting_for_amount)
async def expense_amount(message: Message, state: FSMContext) -> None:
    amount = _parse_decimal(message.text)
    if amount is None or amount <= 0:
        await message.answer(msg.EXPENSE_AMOUNT_INVALID)
        return
    await state.update_data(amount=str(amount))

    # Для категории «Прочее» спрашиваем короткое описание (Блок C) — что за расход.
    data = await state.get_data()
    if data.get("category") == "other":
        await state.set_state(NewExpense.waiting_for_description)
        await message.answer(msg.EXPENSE_ASK_DESCRIPTION)
        return

    await state.set_state(NewExpense.waiting_for_receipt)
    await message.answer(
        msg.EXPENSE_ASK_RECEIPT, reply_markup=kb.expense_receipt_skip_keyboard()
    )


@driver_router.message(NewExpense.waiting_for_description)
async def expense_description(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(msg.EXPENSE_ASK_DESCRIPTION)
        return
    await state.update_data(description=text)
    await state.set_state(NewExpense.waiting_for_receipt)
    await message.answer(
        msg.EXPENSE_ASK_RECEIPT, reply_markup=kb.expense_receipt_skip_keyboard()
    )


async def _finalize_expense(
    *,
    state: FSMContext,
    session: AsyncSession,
    driver: Driver,
    receipt_file_id: str | None,
    source_bot: Bot,
    owner_bot: Bot,
    reply_target: Message,
) -> None:
    data = await state.get_data()
    # Смена не обязательна (Правка 3): расход может быть вне смены — shift_id=None.
    shift = await shift_service.get_active_shift(session, driver.id)
    trip = await trip_service.get_active_trip(session, shift.id) if shift else None
    amount = Decimal(data["amount"])
    category = data["category"]
    description = data.get("description")

    expense = await expense_service.create_expense(
        session,
        owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id if shift else None, trip_id=trip.id if trip else None,
        category=category, amount_rub=amount,
        receipt_photo_id=receipt_file_id,
        description=description,
    )
    await session.flush()
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id if shift else None, trip_id=trip.id if trip else None,
        event_type="expense_submitted",
        payload={"expense_id": expense.id, "category": category, "amount": str(amount)},
    )
    await session.commit()
    await state.clear()

    # подсказка по литрам для топлива
    liters_hint = ""
    if category == "fuel":
        liters = trip_service.liters_from_rub(amount)
        liters_hint = f" (~{liters:.0f} л)"

    await _refresh_ui(
        reply_target, session, driver,
        msg.EXPENSE_SUBMITTED + (f"\n\nТопливо: {amount:.0f} ₽{liters_hint}" if category == "fuel" else ""),
    )

    owner = await session.get(Owner, driver.owner_id)
    if owner is None:
        return

    caption = msg.NOTIFY_EXPENSE.format(
        driver=driver.full_name,
        category=expense_service.CATEGORY_LABELS[category],
        amount=f"{amount:.0f}" + liters_hint,
    )
    if description:
        caption += f"\nОписание: {description}"
    markup = kb.expense_decision_keyboard(expense.id)

    if receipt_file_id is not None:
        await transfer_photo_to_owner(
            source_bot=source_bot, owner_bot=owner_bot,
            session=session, owner=owner,
            source_file_id=receipt_file_id, caption=caption,
            reply_markup=markup,
        )
    else:
        await notify_owner(owner_bot, session, owner, caption, reply_markup=markup)


@driver_router.message(NewExpense.waiting_for_receipt, F.photo)
async def expense_receipt_photo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    owner_bot: Bot,
) -> None:
    file_id = _pick_photo_file_id(message)
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return

    # (Дормант) распознавание суммы с чека. Активно только при включённом
    # FEATURE_RECEIPT_OCR и заданном ключе — иначе остаётся сумма от водителя.
    if receipt_ocr.is_enabled() and file_id is not None:
        try:
            buf = await bot.download(file_id)
            reading = await receipt_ocr.recognize(buf.read())
            if reading and reading.amount_rub:
                await state.update_data(amount=str(reading.amount_rub))
        except Exception as exc:  # noqa: BLE001 — OCR не критичен
            logger.debug("receipt OCR skipped: %s", exc)

    await _finalize_expense(
        state=state, session=session, driver=driver,
        receipt_file_id=file_id, source_bot=bot, owner_bot=owner_bot,
        reply_target=message,
    )


@driver_router.callback_query(NewExpense.waiting_for_receipt, F.data == "exp_receipt:skip")
async def cb_expense_skip_receipt(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    owner_bot: Bot,
) -> None:
    driver = await _driver_by_telegram(session, call.from_user.id)
    if driver is None:
        await state.clear()
        await call.answer(msg.SOMETHING_WRONG, show_alert=True)
        return
    await call.message.delete()
    await _finalize_expense(
        state=state, session=session, driver=driver,
        receipt_file_id=None, source_bot=bot, owner_bot=owner_bot,
        reply_target=call.message,
    )
    await call.answer()


@driver_router.message(NewExpense.waiting_for_receipt)
async def expense_receipt_invalid(message: Message) -> None:
    await message.answer(msg.EXPENSE_RECEIPT_PHOTO_REQUIRED)


# =========================================================================
# ПРОСТОЙ
# =========================================================================
@driver_router.message(F.text == kb.BTN_DOWNTIME, StateFilter(any_state))
async def btn_downtime(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    if await shift_service.get_active_shift(session, driver.id) is not None:
        await _refresh_ui(message, session, driver, "Простой нельзя зафиксировать во время смены.")
        return
    await message.answer(msg.DOWNTIME_ASK_REASON, reply_markup=kb.downtime_reason_keyboard())


@driver_router.callback_query(F.data == "dt:cancel")
async def cb_downtime_cancel(call: CallbackQuery, session: AsyncSession) -> None:
    driver = await _driver_by_telegram(session, call.from_user.id)
    await call.message.delete()
    if driver is not None:
        await _refresh_ui(call.message, session, driver, msg.DOWNTIME_CANCELLED)
    await call.answer()


@driver_router.callback_query(F.data.startswith("dt:"))
async def cb_downtime_reason(
    call: CallbackQuery, session: AsyncSession, owner_bot: Bot
) -> None:
    reason_code = call.data.split(":", 1)[1]
    if reason_code not in kb.DOWNTIME_REASONS:
        await call.answer("Неизвестная причина", show_alert=True)
        return
    driver = await _driver_by_telegram(session, call.from_user.id)
    if driver is None:
        await call.answer(msg.DRIVER_LINK_EXPECTED, show_alert=True)
        return

    reason_label = kb.DOWNTIME_REASONS[reason_code]
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        event_type="downtime",
        payload={"reason": reason_code, "label": reason_label},
    )
    await session.commit()
    await call.message.delete()
    await _refresh_ui(call.message, session, driver, msg.DOWNTIME_RECORDED)

    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            f"⏸ <b>{driver.full_name}</b> отметил простой: {reason_label}",
        )
    await call.answer()


# =========================================================================
# СДАЛ ДЕНЬГИ
# =========================================================================
@driver_router.message(F.text == kb.BTN_HANDED_CASH, StateFilter(any_state))
async def btn_handed_cash(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    if await shift_service.get_active_shift(session, driver.id) is not None:
        await _refresh_ui(message, session, driver, msg.CASH_NEED_NO_SHIFT)
        return
    await state.set_state(HandedCash.waiting_for_amount)
    await message.answer(msg.CASH_ASK_AMOUNT)


@driver_router.message(HandedCash.waiting_for_amount)
async def cash_amount(message: Message, state: FSMContext) -> None:
    amount = _parse_decimal(message.text)
    if amount is None or amount <= 0:
        await message.answer(msg.CASH_AMOUNT_INVALID)
        return
    await state.update_data(amount=str(amount))
    await state.set_state(HandedCash.waiting_for_photo)
    await message.answer(
        msg.CASH_ASK_PHOTO,
        reply_markup=kb.skip_or_cancel_inline("cash:skip"),
    )


async def _submit_cash(
    *,
    state: FSMContext,
    session: AsyncSession,
    driver: Driver,
    photo_file_id: str | None,
    source_bot: Bot,
    owner_bot: Bot,
    reply_target: Message,
) -> None:
    data = await state.get_data()
    amount = Decimal(data["amount"])
    token = uuid4().hex
    CASH_PENDING[token] = {
        "driver_id": driver.id,
        "owner_id": driver.owner_id,
        "amount": str(amount),
        "photo_file_id": photo_file_id,
    }
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        event_type="cash_submitted",
        payload={"token": token, "amount": str(amount)},
    )
    await session.commit()
    await state.clear()

    await _refresh_ui(reply_target, session, driver, msg.CASH_SUBMITTED)

    owner = await session.get(Owner, driver.owner_id)
    if owner is None:
        return

    caption = msg.CASH_NOTIFY_OWNER.format(driver=driver.full_name, amount=f"{amount:.0f}")
    markup = kb.cash_decision_keyboard(token)

    if photo_file_id is not None:
        await transfer_photo_to_owner(
            source_bot=source_bot, owner_bot=owner_bot,
            session=session, owner=owner,
            source_file_id=photo_file_id, caption=caption,
            reply_markup=markup,
        )
    else:
        await notify_owner(owner_bot, session, owner, caption, reply_markup=markup)


@driver_router.message(HandedCash.waiting_for_photo, F.photo)
async def cash_photo(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    owner_bot: Bot,
) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return
    await _submit_cash(
        state=state, session=session, driver=driver,
        photo_file_id=_pick_photo_file_id(message),
        source_bot=bot, owner_bot=owner_bot, reply_target=message,
    )


@driver_router.callback_query(HandedCash.waiting_for_photo, F.data == "cash:skip")
async def cb_cash_skip(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    owner_bot: Bot,
) -> None:
    driver = await _driver_by_telegram(session, call.from_user.id)
    if driver is None:
        await state.clear()
        await call.answer(msg.SOMETHING_WRONG, show_alert=True)
        return
    await call.message.delete()
    await _submit_cash(
        state=state, session=session, driver=driver,
        photo_file_id=None,
        source_bot=bot, owner_bot=owner_bot, reply_target=call.message,
    )
    await call.answer()


@driver_router.message(HandedCash.waiting_for_photo)
async def cash_photo_invalid(message: Message) -> None:
    await message.answer(msg.CASH_ASK_PHOTO)


# =========================================================================
# SOS
# =========================================================================
@driver_router.message(F.text == kb.BTN_SOS, StateFilter(any_state))
async def btn_sos(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    await message.answer(msg.SOS_ASK_CONFIRM, reply_markup=kb.sos_confirm_keyboard())


@driver_router.callback_query(F.data == "sos:cancel")
async def cb_sos_cancel(call: CallbackQuery, session: AsyncSession) -> None:
    driver = await _driver_by_telegram(session, call.from_user.id)
    await call.message.delete()
    if driver is not None:
        await _refresh_ui(call.message, session, driver, msg.SOS_CANCELLED)
    await call.answer()


@driver_router.callback_query(F.data == "sos:confirm")
async def cb_sos_confirm(
    call: CallbackQuery, session: AsyncSession, owner_bot: Bot
) -> None:
    driver = await _driver_by_telegram(session, call.from_user.id)
    if driver is None:
        await call.answer(msg.DRIVER_LINK_EXPECTED, show_alert=True)
        return

    shift = await shift_service.get_active_shift(session, driver.id)
    trip = await trip_service.get_active_trip(session, shift.id) if shift else None
    vehicle = await session.get(Vehicle, shift.vehicle_id) if shift else None

    if trip is not None:
        _st = {"created": "создан", "in_transit": "в пути", "unloading": "на выгрузке"}.get(
            trip.status, trip.status
        )
        state_descr = f"рейс {_st}, {trip.origin or '—'} → {trip.destination or '—'}"
    elif shift is not None:
        state_descr = "в смене, без активного рейса"
    else:
        state_descr = "вне смены"

    await log_event(
        session,
        owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id if shift else None,
        trip_id=trip.id if trip else None,
        event_type="sos",
        payload={"state": state_descr},
    )
    await session.commit()

    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_SOS.format(
                driver=driver.full_name,
                plate=vehicle.license_plate if vehicle else "—",
                phone=driver.phone or "—",
                state=state_descr,
            ),
        )

    await call.message.delete()
    await _refresh_ui(call.message, session, driver, msg.SOS_SENT)
    await call.answer()


# =========================================================================
# ОФФЛАЙН-ДОБАВЛЕНИЕ задним числом (Блок D)
# Когда на складе не было связи: водитель вносит смену/рейс позже.
# У таких записей нет одометра/GPS — пробег неизвестен, помечаем is_manual.
# =========================================================================
async def _owner_active_vehicles(session: AsyncSession, owner_id: int) -> list[Vehicle]:
    res = await session.execute(
        select(Vehicle)
        .where(Vehicle.owner_id == owner_id, Vehicle.is_active.is_(True))
        .order_by(Vehicle.license_plate)
    )
    return list(res.scalars().all())


_MANUAL_DATE_RE = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$")


def _parse_manual_date(text: str | None, tz_name: str | None) -> datetime | None:
    """«сегодня»/«вчера»/«ДД.ММ»/«ДД.ММ.ГГГГ» → UTC-aware (полдень местного времени)."""
    tz = owner_tz(tz_name)
    today = datetime.now(tz).date()
    raw = (text or "").strip().lower()
    if raw in ("", "сегодня"):
        d = today
    elif raw == "вчера":
        d = today - timedelta(days=1)
    else:
        m = _MANUAL_DATE_RE.match(raw)
        if not m:
            return None
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            d = date(year, month, day)
        except ValueError:
            return None
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=tz).astimezone(timezone.utc)


def _date_label(dt: datetime, tz_name: str | None) -> str:
    return dt.astimezone(owner_tz(tz_name)).strftime("%d.%m.%Y")


# --- Добавить смену вручную ---
@driver_router.message(F.text == kb.BTN_ADD_SHIFT, StateFilter(any_state))
async def btn_add_shift(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    vehicles = await _owner_active_vehicles(session, driver.owner_id)
    if not vehicles:
        await _refresh_ui(message, session, driver, msg.MANUAL_NO_VEHICLES)
        return
    if len(vehicles) == 1:
        await state.update_data(vehicle_id=vehicles[0].id)
        await state.set_state(AddManualShift.waiting_for_date)
        await message.answer(msg.MANUAL_ASK_DATE)
        return
    await state.set_state(AddManualShift.selecting_vehicle)
    await message.answer(
        msg.MANUAL_PICK_VEHICLE, reply_markup=kb.manual_vehicle_keyboard(vehicles, "mshift")
    )


@driver_router.callback_query(AddManualShift.selecting_vehicle, F.data == "mshift:cancel")
async def cb_mshift_cancel(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, call.from_user.id)
    await call.message.delete()
    if driver is not None:
        await _refresh_ui(call.message, session, driver, msg.CANCELLED)
    await call.answer()


@driver_router.callback_query(AddManualShift.selecting_vehicle, F.data.startswith("mshift:veh:"))
async def cb_mshift_vehicle(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    vehicle_id = int(call.data.split(":")[2])
    vehicle = await session.get(Vehicle, vehicle_id)
    driver = await _driver_by_telegram(session, call.from_user.id)
    if vehicle is None or driver is None or vehicle.owner_id != driver.owner_id:
        await call.answer("Машина недоступна", show_alert=True)
        return
    await state.update_data(vehicle_id=vehicle_id)
    await state.set_state(AddManualShift.waiting_for_date)
    await call.message.edit_text(f"Машина: <b>{vehicle.license_plate}</b>")
    await call.message.answer(msg.MANUAL_ASK_DATE)
    await call.answer()


@driver_router.message(AddManualShift.waiting_for_date)
async def manual_shift_date(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return
    owner = await session.get(Owner, driver.owner_id)
    tz_name = owner.timezone if owner else None
    dt = _parse_manual_date(message.text, tz_name)
    if dt is None:
        await message.answer(msg.MANUAL_DATE_INVALID)
        return
    data = await state.get_data()
    vehicle = await session.get(Vehicle, data.get("vehicle_id"))
    if vehicle is None or vehicle.owner_id != driver.owner_id:
        await state.clear()
        await _refresh_ui(message, session, driver, msg.SOMETHING_WRONG)
        return

    shift = Shift(
        owner_id=driver.owner_id, driver_id=driver.id, vehicle_id=vehicle.id,
        status="completed", started_at=dt, ended_at=dt, is_manual=True,
    )
    session.add(shift)
    await session.flush()
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id, shift_id=shift.id,
        event_type="shift_added_manual",
        payload={"vehicle_id": vehicle.id, "date": dt.isoformat()},
    )
    await session.commit()
    await state.clear()

    label = _date_label(dt, tz_name)
    await _refresh_ui(message, session, driver, msg.MANUAL_SHIFT_DONE.format(date=label))
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_MANUAL_SHIFT.format(
                driver=driver.full_name, date=label, plate=vehicle.license_plate
            ),
        )


# --- Добавить рейс вручную ---
async def _manual_trip_ask_route(
    reply_target: Message, state: FSMContext, session: AsyncSession, owner_id: int
) -> None:
    templates_res = await session.execute(
        select(RouteTemplate)
        .where(RouteTemplate.owner_id == owner_id, RouteTemplate.is_active.is_(True))
        .order_by(RouteTemplate.name)
    )
    templates = list(templates_res.scalars().all())
    if templates:
        await state.set_state(AddManualTrip.waiting_for_origin)
        await state.update_data(picking_template=True)
        await reply_target.answer(
            "Маршрут — выберите шаблон или введите свой:",
            reply_markup=kb.manual_route_keyboard(templates),
        )
    else:
        await state.set_state(AddManualTrip.waiting_for_origin)
        await reply_target.answer(msg.TRIP_ASK_ORIGIN)


@driver_router.message(F.text == kb.BTN_ADD_TRIP, StateFilter(any_state))
async def btn_add_trip(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    vehicles = await _owner_active_vehicles(session, driver.owner_id)
    if not vehicles:
        await _refresh_ui(message, session, driver, msg.MANUAL_NO_VEHICLES)
        return
    if len(vehicles) == 1:
        await state.update_data(vehicle_id=vehicles[0].id)
        await _manual_trip_ask_route(message, state, session, driver.owner_id)
        return
    await state.set_state(AddManualTrip.selecting_vehicle)
    await message.answer(
        msg.MANUAL_PICK_VEHICLE, reply_markup=kb.manual_vehicle_keyboard(vehicles, "mtrip")
    )


@driver_router.callback_query(F.data == "mtrip:cancel")
async def cb_mtrip_cancel(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, call.from_user.id)
    await call.message.delete()
    if driver is not None:
        await _refresh_ui(call.message, session, driver, msg.CANCELLED)
    await call.answer()


@driver_router.callback_query(AddManualTrip.selecting_vehicle, F.data.startswith("mtrip:veh:"))
async def cb_mtrip_vehicle(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    vehicle_id = int(call.data.split(":")[2])
    vehicle = await session.get(Vehicle, vehicle_id)
    driver = await _driver_by_telegram(session, call.from_user.id)
    if vehicle is None or driver is None or vehicle.owner_id != driver.owner_id:
        await call.answer("Машина недоступна", show_alert=True)
        return
    await state.update_data(vehicle_id=vehicle_id)
    await call.message.edit_text(f"Машина: <b>{vehicle.license_plate}</b>")
    await _manual_trip_ask_route(call.message, state, session, driver.owner_id)
    await call.answer()


@driver_router.callback_query(F.data.startswith("mtrip:rt:"))
async def cb_mtrip_route_template(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    template_id = int(call.data.split(":")[2])
    template = await session.get(RouteTemplate, template_id)
    driver = await _driver_by_telegram(session, call.from_user.id)
    if driver is None or template is None or template.owner_id != driver.owner_id:
        await call.answer("Шаблон недоступен", show_alert=True)
        return
    await state.update_data(origin=template.origin, destination=template.destination)
    await state.set_state(AddManualTrip.waiting_for_date)
    await call.message.edit_text(f"Маршрут: <b>{template.origin} → {template.destination}</b>")
    await call.message.answer(msg.MANUAL_ASK_DATE)
    await call.answer()


@driver_router.callback_query(F.data == "mtrip:manual")
async def cb_mtrip_manual(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddManualTrip.waiting_for_origin)
    await state.update_data(picking_template=False)
    await call.message.delete()
    await call.message.answer(msg.TRIP_ASK_ORIGIN)
    await call.answer()


@driver_router.message(AddManualTrip.waiting_for_origin)
async def manual_trip_origin(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("picking_template"):
        await state.update_data(picking_template=False)
    text = (message.text or "").strip()
    if not text:
        await message.answer(msg.TRIP_ASK_ORIGIN)
        return
    await state.update_data(origin=text)
    await state.set_state(AddManualTrip.waiting_for_destination)
    await message.answer(msg.TRIP_ASK_DESTINATION)


@driver_router.message(AddManualTrip.waiting_for_destination)
async def manual_trip_destination(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(msg.TRIP_ASK_DESTINATION)
        return
    await state.update_data(destination=text)
    await state.set_state(AddManualTrip.waiting_for_date)
    await message.answer(msg.MANUAL_ASK_DATE)


@driver_router.message(AddManualTrip.waiting_for_date)
async def manual_trip_date(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return
    owner = await session.get(Owner, driver.owner_id)
    tz_name = owner.timezone if owner else None
    dt = _parse_manual_date(message.text, tz_name)
    if dt is None:
        await message.answer(msg.MANUAL_DATE_INVALID)
        return
    data = await state.get_data()
    vehicle = await session.get(Vehicle, data.get("vehicle_id"))
    origin, destination = data.get("origin"), data.get("destination")
    if vehicle is None or vehicle.owner_id != driver.owner_id or not origin or not destination:
        await state.clear()
        await _refresh_ui(message, session, driver, msg.SOMETHING_WRONG)
        return

    # Ручной рейс живёт в ручной (завершённой) смене — чтобы FK shift_id был валиден.
    shift = Shift(
        owner_id=driver.owner_id, driver_id=driver.id, vehicle_id=vehicle.id,
        status="completed", started_at=dt, ended_at=dt, is_manual=True,
    )
    session.add(shift)
    await session.flush()
    trip = await trip_service.create_trip(
        session, shift=shift, origin=origin, destination=destination, cargo_name=None,
    )
    await session.flush()
    trip.status = "completed"
    trip.completed_at = dt
    trip.is_manual = True
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_added_manual",
        payload={"origin": origin, "destination": destination, "date": dt.isoformat()},
    )
    await session.commit()
    await state.clear()

    label = _date_label(dt, tz_name)
    await _refresh_ui(
        message, session, driver,
        msg.MANUAL_TRIP_DONE.format(date=label, origin=origin, destination=destination),
    )
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_MANUAL_TRIP.format(
                driver=driver.full_name, date=label, origin=origin,
                destination=destination, plate=vehicle.license_plate,
            ),
        )


# =========================================================================
# Fallback
# =========================================================================
@driver_router.callback_query()
async def fallback_callback(call: CallbackQuery) -> None:
    """
    Срабатывает на «устаревшие» inline-кнопки, которые больше не привязаны
    к нужному FSM-состоянию (например, после рестарта процесса состояние
    в памяти сбросилось, а кнопка в Telegram осталась). Без этого aiogram
    просто не отвечает на callback, и Telegram через 10с показывает
    дефолтный alert «Произошла ошибка».
    """
    await call.answer(
        "Эта кнопка устарела. Нажмите /status или /start.",
        show_alert=True,
    )


@driver_router.message()
async def fallback(message: Message, session: AsyncSession) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    await _refresh_ui(message, session, driver, msg.UNKNOWN_COMMAND)
