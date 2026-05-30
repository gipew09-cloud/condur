"""
Бот ВОДИТЕЛЯ. Этап 2+ — полный цикл смены и рейсов.

Архитектурный принцип:
  FSM — только подсказка для UI. Источник истины — БД.
  В начале каждого хендлера сначала проверяем БД, потом ориентируемся
  на FSM. /status — главная защита: пересоздаёт правильное UI-состояние
  из БД и сбрасывает залипший FSM.
"""
import logging
from datetime import datetime, timezone
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
from app.bots.states import (
    EndShift,
    EndTripLocation,
    HandedCash,
    NewExpense,
    NewTrip,
    StartShift,
    UnloadingLocation,
    UploadWaybill,
)
from app.models import Driver, Expense, Owner, RouteTemplate, Shift, Vehicle
from app.services import (
    expense_service,
    salary_service,
    shift_service,
    trip_service,
)
from app.services.cash_pending import PENDING as CASH_PENDING
from app.services.event_service import log_event
from app.services.timeutil import fmt_time

logger = logging.getLogger(__name__)
driver_router = Router()


# =========================================================================
# Хелперы
# =========================================================================
async def _driver_by_telegram(session: AsyncSession, telegram_id: int) -> Driver | None:
    result = await session.execute(select(Driver).where(Driver.telegram_id == telegram_id))
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
# /help, /status, /cancel, /balance
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


@driver_router.message(Command("balance"), StateFilter(any_state))
async def cmd_balance(message: Message, session: AsyncSession) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return

    # Завершённые смены текущего месяца
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    shifts_res = await session.execute(
        select(Shift).where(
            Shift.driver_id == driver.id,
            Shift.status == "completed",
            Shift.ended_at >= month_start,
        )
    )
    shifts = list(shifts_res.scalars().all())

    earned = Decimal(0)
    for sh in shifts:
        trips = await shift_service.get_shift_trips(session, sh.id)
        earned += salary_service.calculate_salary(driver, sh, trips)

    expenses_res = await session.execute(
        select(Expense).where(
            Expense.driver_id == driver.id,
            Expense.status == "approved",
            Expense.created_at >= month_start,
        )
    )
    expenses_sum = sum(
        (e.amount_rub or Decimal(0)) for e in expenses_res.scalars().all()
    ) or Decimal(0)

    await message.answer(
        msg.DRIVER_BALANCE.format(
            earned=f"{earned:.0f}",
            expenses=f"{Decimal(expenses_sum):.0f}",
            total=f"{(earned - Decimal(expenses_sum)):.0f}",
        )
    )


# =========================================================================
# НАЧАЛО СМЕНЫ
# =========================================================================
@driver_router.message(F.text == kb.BTN_START_SHIFT, StateFilter(any_state))
async def btn_start_shift(message: Message, state: FSMContext, session: AsyncSession) -> None:
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


@driver_router.callback_query(StartShift.selecting_vehicle, F.data.startswith("shift:pick:"))
async def cb_shift_pick_vehicle(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    vehicle_id = int(call.data.split(":")[2])
    vehicle = await session.get(Vehicle, vehicle_id)
    driver = await _driver_by_telegram(session, call.from_user.id)
    if vehicle is None or driver is None or vehicle.owner_id != driver.owner_id:
        await call.answer("Машина недоступна", show_alert=True)
        return

    await state.update_data(vehicle_id=vehicle.id)
    await state.set_state(StartShift.waiting_for_odometer_photo)
    await call.message.edit_text(f"Машина: <b>{vehicle.license_plate}</b>")
    await call.message.answer(msg.SHIFT_ASK_ODOMETER_PHOTO_START + " 📷")
    await call.answer()


@driver_router.message(StartShift.waiting_for_odometer_photo, F.photo)
async def shift_start_odometer_photo(message: Message, state: FSMContext) -> None:
    file_id = _pick_photo_file_id(message)
    await state.update_data(odometer_photo=file_id)
    await state.set_state(StartShift.waiting_for_odometer_value)
    await message.answer(msg.SHIFT_ASK_ODOMETER_VALUE)


@driver_router.message(StartShift.waiting_for_odometer_photo)
async def shift_start_odometer_photo_invalid(message: Message) -> None:
    await message.answer(msg.SHIFT_ASK_ODOMETER_PHOTO_START + " 📷")


@driver_router.message(StartShift.waiting_for_odometer_value)
async def shift_start_odometer_value(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    value = _parse_int(message.text)
    if value is None or value < 0:
        await message.answer(msg.SHIFT_ODOMETER_INVALID)
        return

    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return

    data = await state.get_data()
    vehicle = await session.get(Vehicle, data["vehicle_id"])
    if vehicle is None:
        await state.clear()
        await _refresh_ui(message, session, driver, msg.SOMETHING_WRONG)
        return

    shift = await shift_service.start_shift(
        session,
        owner_id=driver.owner_id,
        driver_id=driver.id,
        vehicle_id=vehicle.id,
        odometer_start=value,
        photo_file_id=data.get("odometer_photo"),
    )
    await session.flush()
    await log_event(
        session,
        owner_id=driver.owner_id,
        driver_id=driver.id,
        shift_id=shift.id,
        event_type="shift_started",
        payload={"vehicle_id": vehicle.id, "odometer_start": value},
    )
    await session.commit()
    await state.clear()

    await _refresh_ui(
        message, session, driver,
        msg.SHIFT_STARTED.format(plate=vehicle.license_plate, km=value),
    )

    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_SHIFT_STARTED.format(
                driver=driver.full_name, plate=vehicle.license_plate, km=value
            ),
        )


# =========================================================================
# ЗАВЕРШЕНИЕ СМЕНЫ — с контролем топлива
# =========================================================================
@driver_router.message(F.text == kb.BTN_END_SHIFT, StateFilter(any_state))
async def btn_end_shift(message: Message, state: FSMContext, session: AsyncSession) -> None:
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

    await state.set_state(EndShift.waiting_for_odometer_photo)
    await message.answer(msg.SHIFT_ASK_ODOMETER_PHOTO_END + " 📷")


@driver_router.message(EndShift.waiting_for_odometer_photo, F.photo)
async def shift_end_odometer_photo(message: Message, state: FSMContext) -> None:
    file_id = _pick_photo_file_id(message)
    await state.update_data(odometer_photo=file_id)
    await state.set_state(EndShift.waiting_for_odometer_value)
    await message.answer(msg.SHIFT_ASK_ODOMETER_VALUE)


@driver_router.message(EndShift.waiting_for_odometer_photo)
async def shift_end_odometer_photo_invalid(message: Message) -> None:
    await message.answer(msg.SHIFT_ASK_ODOMETER_PHOTO_END + " 📷")


@driver_router.message(EndShift.waiting_for_odometer_value)
async def shift_end_odometer_value(
    message: Message, state: FSMContext, session: AsyncSession, owner_bot: Bot
) -> None:
    value = _parse_int(message.text)
    if value is None or value < 0:
        await message.answer(msg.SHIFT_ODOMETER_INVALID)
        return

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

    if value < (shift.odometer_start or 0):
        await message.answer(
            msg.SHIFT_ODOMETER_BELOW_START.format(end=value, start=shift.odometer_start)
        )
        return

    data = await state.get_data()
    await shift_service.end_shift(
        session,
        shift=shift,
        odometer_end=value,
        photo_file_id=data.get("odometer_photo"),
        ended_at=datetime.now(timezone.utc),
    )
    await session.flush()
    await session.refresh(shift)

    trips = await shift_service.get_shift_trips(session, shift.id)
    revenue = sum((t.revenue_rub or Decimal(0)) for t in trips) or Decimal(0)

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

    await _refresh_ui(
        message, session, driver,
        msg.SHIFT_COMPLETED_DRIVER.format(
            distance=shift.distance_km or 0,
            trips=len(trips),
            expenses=f"{expenses_total:.2f}",
            salary=f"{salary:.0f}",
        ),
    )

    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_SHIFT_COMPLETED.format(
                driver=driver.full_name,
                distance=shift.distance_km or 0,
                trips=len(trips),
                revenue=f"{revenue:.0f}",
                expenses=f"{expenses_total:.0f}",
                salary=f"{salary:.0f}",
            ),
        )
        # Контроль топлива: сумма fuel-расходов смены vs норма
        await _maybe_fuel_overrun_alert(session, owner_bot, owner, driver, shift, approved_list)


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
    call: CallbackQuery, state: FSMContext, session: AsyncSession, owner_bot: Bot
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

    trip = await trip_service.create_trip(
        session,
        shift=shift,
        origin=template.origin,
        destination=template.destination,
        cargo_name=template.default_cargo or template.name,
    )
    await session.flush()
    await log_event(
        session,
        owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id,
        event_type="trip_created",
        payload={"template_id": template.id},
    )
    await session.commit()
    await state.clear()

    await call.message.delete()
    await _refresh_ui(
        call.message, session, driver,
        msg.TRIP_CREATED.format(
            origin=template.origin, destination=template.destination,
            cargo=template.default_cargo or template.name,
        ),
    )
    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_TRIP_CREATED.format(
                driver=driver.full_name, origin=template.origin,
                destination=template.destination,
                cargo=template.default_cargo or template.name,
            ),
        )
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
async def trip_destination(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(msg.TRIP_ASK_DESTINATION)
        return
    await state.update_data(destination=text)
    await state.set_state(NewTrip.waiting_for_cargo)
    await message.answer(msg.TRIP_ASK_CARGO)


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
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        await state.clear()
        await _refresh_ui(message, session, driver, msg.TRIP_NEED_SHIFT)
        return

    data = await state.get_data()
    trip = await trip_service.create_trip(
        session, shift=shift,
        origin=data["origin"], destination=data["destination"], cargo_name=cargo,
    )
    await session.flush()
    await log_event(
        session,
        owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_created",
        payload={"origin": data["origin"], "destination": data["destination"]},
    )
    await session.commit()
    await state.clear()

    await _refresh_ui(
        message, session, driver,
        msg.TRIP_CREATED.format(
            origin=data["origin"], destination=data["destination"], cargo=cargo
        ),
    )
    owner = await session.get(Owner, driver.owner_id)
    if owner is not None:
        await notify_owner(
            owner_bot, session, owner,
            msg.NOTIFY_TRIP_CREATED.format(
                driver=driver.full_name, origin=data["origin"],
                destination=data["destination"], cargo=cargo,
            ),
        )


# =========================================================================
# ВЫЕХАЛ (created → in_transit, без геопозиции)
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

    await trip_service.set_trip_status(session, trip=trip, status="in_transit")
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="trip_in_transit",
    )
    await session.commit()
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
@driver_router.message(F.text == kb.BTN_TRIP_UNLOADING, StateFilter(any_state))
async def btn_trip_unloading_start(
    message: Message, state: FSMContext, session: AsyncSession
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
    if trip is None or trip.status == "created":
        await _refresh_ui(message, session, driver, msg.TRIP_WRONG_STATUS)
        return

    await state.set_state(UploadWaybill.waiting_for_photo)
    await message.answer("Сфотографируйте документ 📷")


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


@driver_router.message(UploadWaybill.waiting_for_photo)
async def upload_waybill_invalid(message: Message) -> None:
    await message.answer(msg.WAYBILL_PHOTO_REQUIRED)


# =========================================================================
# СДАЛ ГРУЗ (unloading → completed) — с геопозицией, без выручки/литров
# =========================================================================
@driver_router.message(F.text == kb.BTN_END_TRIP, StateFilter(any_state))
async def btn_end_trip_start(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    trip = await trip_service.get_active_trip(session, shift.id) if shift else None
    if trip is None or trip.status != "unloading":
        await _refresh_ui(message, session, driver, msg.TRIP_WRONG_STATUS)
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
    if trip is None or trip.status != "unloading":
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
# РАСХОДЫ (для fuel показываем литры)
# =========================================================================
@driver_router.message(F.text == kb.BTN_EXPENSE, StateFilter(any_state))
async def btn_expense(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        await _refresh_ui(message, session, driver, msg.SHIFT_NO_ACTIVE)
        return

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
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        await state.clear()
        await _refresh_ui(reply_target, session, driver, msg.SHIFT_NO_ACTIVE)
        return
    trip = await trip_service.get_active_trip(session, shift.id)
    amount = Decimal(data["amount"])
    category = data["category"]

    expense = await expense_service.create_expense(
        session,
        owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id if trip else None,
        category=category, amount_rub=amount,
        receipt_photo_id=receipt_file_id,
    )
    await session.flush()
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id if trip else None,
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
        state_descr = f"рейс {trip.status}, {trip.origin or '—'} → {trip.destination or '—'}"
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
# Fallback
# =========================================================================
@driver_router.message()
async def fallback(message: Message, session: AsyncSession) -> None:
    driver = await _driver_by_telegram(session, message.from_user.id)
    if driver is None:
        await message.answer(msg.DRIVER_LINK_EXPECTED)
        return
    await _refresh_ui(message, session, driver, msg.UNKNOWN_COMMAND)
