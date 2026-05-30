"""
Бот ВЛАДЕЛЬЦА автопарка.

Этапы 1–2:
  - /start: регистрация или вход в главное меню
  - главное меню (inline): «Мои водители», «Мои машины», «Статистика»
  - добавить водителя через FSM → сгенерировать invite-ссылку
  - добавить машину через FSM
  - показать список водителей / машин с пометкой кто сейчас в смене
  - inline-callback'и одобрения/отклонения расходов (Этап 2)

Принцип: FSM — только подсказка для UI. Источник истины — БД.
В начале каждого хендлера сначала смотрим, что в БД, потом ориентируемся
на состояние FSM.
"""
import logging
import re
import uuid
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import any_state
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import messages as msg
from app.bots.keyboards import (
    back_to_menu_keyboard,
    driver_salary_type_keyboard,
    drivers_list_keyboard,
    owner_main_menu,
    vehicle_type_keyboard,
    vehicles_list_keyboard,
)
from app.bots.notifications import notify_driver
from app.bots.states import AddDriver, AddVehicle, OwnerRegistration
from app.models import Driver, Owner, Shift, Vehicle
from app.services import auth_service, expense_service
from app.services.event_service import log_event

logger = logging.getLogger(__name__)
owner_router = Router()

PHONE_RE = re.compile(r"^\+7\d{10}$")


async def _get_owner(session: AsyncSession, telegram_id: int) -> Owner | None:
    result = await session.execute(select(Owner).where(Owner.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def _show_main_menu(message: Message, owner: Owner) -> None:
    company = owner.company_name or "ваш автопарк"
    await message.answer(
        msg.OWNER_WELCOME_BACK.format(company=company),
        reply_markup=owner_main_menu(),
    )


# =========================================================================
# /start — регистрация или вход
# =========================================================================
@owner_router.message(CommandStart(), StateFilter(any_state))
async def cmd_start(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    owner = await _get_owner(session, message.from_user.id)
    if owner is not None:
        await _show_main_menu(message, owner)
        return

    await state.set_state(OwnerRegistration.waiting_for_company)
    await message.answer(msg.OWNER_WELCOME_NEW)


@owner_router.message(OwnerRegistration.waiting_for_company)
async def reg_company(message: Message, state: FSMContext) -> None:
    company = (message.text or "").strip()
    if not company:
        await message.answer("Название не может быть пустым. Введите название компании.")
        return
    await state.update_data(company_name=company)
    await state.set_state(OwnerRegistration.waiting_for_phone)
    await message.answer(msg.OWNER_ASK_PHONE)


@owner_router.message(OwnerRegistration.waiting_for_phone)
async def reg_phone(message: Message, state: FSMContext, session: AsyncSession) -> None:
    phone = (message.text or "").strip()
    if not PHONE_RE.match(phone):
        await message.answer(msg.OWNER_INVALID_PHONE)
        return

    data = await state.get_data()
    owner = Owner(
        telegram_id=message.from_user.id,
        full_name=message.from_user.full_name,
        company_name=data["company_name"],
        phone=phone,
    )
    session.add(owner)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _get_owner(session, message.from_user.id)
        if existing is not None:
            await state.clear()
            await _show_main_menu(message, existing)
            return
        logger.exception("Owner registration failed")
        await message.answer(msg.SOMETHING_WRONG)
        return

    await state.clear()
    await message.answer(msg.OWNER_REGISTERED)
    await _show_main_menu(message, owner)


# =========================================================================
# Главное меню — переходы по callback
# =========================================================================
@owner_router.callback_query(F.data == "owner:menu")
async def cb_main_menu(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    owner = await _get_owner(session, call.from_user.id)
    if owner is None:
        await call.answer("Сначала /start", show_alert=True)
        return
    company = owner.company_name or "ваш автопарк"
    await call.message.edit_text(
        msg.OWNER_WELCOME_BACK.format(company=company),
        reply_markup=owner_main_menu(),
    )
    await call.answer()


@owner_router.callback_query(F.data == "owner:stats")
async def cb_stats(call: CallbackQuery) -> None:
    await call.message.edit_text(msg.STATS_PLACEHOLDER, reply_markup=back_to_menu_keyboard())
    await call.answer()


# =========================================================================
# Водители — список
# =========================================================================
async def _active_driver_ids(session: AsyncSession, owner_id: int) -> set[int]:
    result = await session.execute(
        select(Shift.driver_id).where(Shift.owner_id == owner_id, Shift.status == "started")
    )
    return {row[0] for row in result.all()}


@owner_router.callback_query(F.data == "owner:drivers")
async def cb_drivers(call: CallbackQuery, session: AsyncSession) -> None:
    owner = await _get_owner(session, call.from_user.id)
    if owner is None:
        await call.answer("Сначала /start", show_alert=True)
        return

    drivers_res = await session.execute(
        select(Driver).where(Driver.owner_id == owner.id, Driver.is_active.is_(True)).order_by(Driver.full_name)
    )
    drivers = list(drivers_res.scalars().all())
    if not drivers:
        await call.message.edit_text(
            msg.DRIVERS_EMPTY,
            reply_markup=drivers_list_keyboard([], set()),
        )
        await call.answer()
        return

    active_ids = await _active_driver_ids(session, owner.id)
    text_lines = [msg.DRIVERS_LIST_HEADER, ""]
    for d in drivers:
        mark = "🟢 в смене" if d.id in active_ids else "⚪️ свободен"
        if d.telegram_id is None:
            mark = "⏳ не активировал ссылку"
        text_lines.append(f"• <b>{d.full_name}</b> — {mark}")
    await call.message.edit_text(
        "\n".join(text_lines),
        reply_markup=drivers_list_keyboard(drivers, active_ids),
    )
    await call.answer()


# =========================================================================
# Водители — добавление
# =========================================================================
@owner_router.callback_query(F.data == "driver:add")
async def cb_add_driver(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddDriver.waiting_for_name)
    await call.message.edit_text(msg.ADD_DRIVER_NAME)
    await call.answer()


@owner_router.message(AddDriver.waiting_for_name)
async def add_driver_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Имя слишком короткое. Введите ФИО водителя.")
        return
    await state.update_data(full_name=name)
    await state.set_state(AddDriver.waiting_for_phone)
    await message.answer(msg.ADD_DRIVER_PHONE)


@owner_router.message(AddDriver.waiting_for_phone)
async def add_driver_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if not PHONE_RE.match(phone):
        await message.answer(msg.OWNER_INVALID_PHONE)
        return
    await state.update_data(phone=phone)
    await state.set_state(AddDriver.waiting_for_salary_type)
    await message.answer(msg.ADD_DRIVER_SALARY_TYPE, reply_markup=driver_salary_type_keyboard())


@owner_router.callback_query(AddDriver.waiting_for_salary_type, F.data.startswith("salary:"))
async def add_driver_salary_type(call: CallbackQuery, state: FSMContext) -> None:
    salary_type = call.data.split(":", 1)[1]
    if salary_type not in ("per_km", "per_trip", "percent", "fixed_per_shift"):
        await call.answer("Неизвестный тип", show_alert=True)
        return
    await state.update_data(salary_type=salary_type)
    await state.set_state(AddDriver.waiting_for_salary_rate)

    prompt = {
        "per_km": msg.ADD_DRIVER_RATE_PER_KM,
        "per_trip": msg.ADD_DRIVER_RATE_PER_TRIP,
        "percent": msg.ADD_DRIVER_RATE_PERCENT,
        "fixed_per_shift": msg.ADD_DRIVER_RATE_FIXED,
    }[salary_type]
    await call.message.edit_text(prompt)
    await call.answer()


@owner_router.message(AddDriver.waiting_for_salary_rate)
async def add_driver_rate(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    driver_bot: Bot,
) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        rate = Decimal(raw)
        if rate < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer(msg.ADD_DRIVER_INVALID_RATE)
        return

    owner = await _get_owner(session, message.from_user.id)
    if owner is None:
        await message.answer(msg.SOMETHING_WRONG)
        await state.clear()
        return

    data = await state.get_data()
    invite_token = uuid.uuid4().hex

    driver = Driver(
        owner_id=owner.id,
        full_name=data["full_name"],
        phone=data["phone"],
        salary_type=data["salary_type"],
        salary_rate=rate,
        invite_token=invite_token,
    )
    session.add(driver)
    await session.commit()
    await state.clear()

    me = await driver_bot.get_me()
    link = f"https://t.me/{me.username}?start={invite_token}"
    await message.answer(msg.ADD_DRIVER_DONE.format(link=link))
    owner_refreshed = await _get_owner(session, message.from_user.id)
    if owner_refreshed is not None:
        await _show_main_menu(message, owner_refreshed)


# =========================================================================
# Машины — список
# =========================================================================
async def _busy_vehicle_ids(session: AsyncSession, owner_id: int) -> set[int]:
    result = await session.execute(
        select(Shift.vehicle_id).where(Shift.owner_id == owner_id, Shift.status == "started")
    )
    return {row[0] for row in result.all()}


@owner_router.callback_query(F.data == "owner:vehicles")
async def cb_vehicles(call: CallbackQuery, session: AsyncSession) -> None:
    owner = await _get_owner(session, call.from_user.id)
    if owner is None:
        await call.answer("Сначала /start", show_alert=True)
        return

    vehicles_res = await session.execute(
        select(Vehicle).where(Vehicle.owner_id == owner.id, Vehicle.is_active.is_(True)).order_by(Vehicle.license_plate)
    )
    vehicles = list(vehicles_res.scalars().all())
    if not vehicles:
        await call.message.edit_text(
            msg.VEHICLES_EMPTY,
            reply_markup=vehicles_list_keyboard([], set()),
        )
        await call.answer()
        return

    busy_ids = await _busy_vehicle_ids(session, owner.id)
    text_lines = [msg.VEHICLES_LIST_HEADER, ""]
    for v in vehicles:
        mark = "🟢 в работе" if v.id in busy_ids else "⚪️ свободна"
        brand = f" — {v.brand}" if v.brand else ""
        text_lines.append(f"• <b>{v.license_plate}</b>{brand} — {mark}")
    await call.message.edit_text(
        "\n".join(text_lines),
        reply_markup=vehicles_list_keyboard(vehicles, busy_ids),
    )
    await call.answer()


# =========================================================================
# Машины — добавление
# =========================================================================
@owner_router.callback_query(F.data == "vehicle:add")
async def cb_add_vehicle(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddVehicle.waiting_for_plate)
    await call.message.edit_text(msg.ADD_VEHICLE_PLATE)
    await call.answer()


@owner_router.message(AddVehicle.waiting_for_plate)
async def add_vehicle_plate(message: Message, state: FSMContext, session: AsyncSession) -> None:
    plate = (message.text or "").strip().upper().replace(" ", "")
    if len(plate) < 6:
        await message.answer("Госномер слишком короткий. Введите ещё раз.")
        return

    owner = await _get_owner(session, message.from_user.id)
    if owner is None:
        await message.answer(msg.SOMETHING_WRONG)
        await state.clear()
        return

    existing = await session.execute(
        select(Vehicle).where(Vehicle.owner_id == owner.id, Vehicle.license_plate == plate)
    )
    if existing.scalar_one_or_none() is not None:
        await message.answer(msg.ADD_VEHICLE_PLATE_EXISTS)
        return

    await state.update_data(license_plate=plate)
    await state.set_state(AddVehicle.waiting_for_brand)
    await message.answer(msg.ADD_VEHICLE_BRAND)


@owner_router.message(AddVehicle.waiting_for_brand)
async def add_vehicle_brand(message: Message, state: FSMContext) -> None:
    brand = (message.text or "").strip()
    if not brand:
        await message.answer("Введите марку и модель.")
        return
    await state.update_data(brand=brand)
    await state.set_state(AddVehicle.waiting_for_type)
    await message.answer(msg.ADD_VEHICLE_TYPE, reply_markup=vehicle_type_keyboard())


@owner_router.callback_query(AddVehicle.waiting_for_type, F.data.startswith("vtype:"))
async def add_vehicle_type(call: CallbackQuery, state: FSMContext) -> None:
    vtype = call.data.split(":", 1)[1]
    if vtype not in ("truck", "gazelle", "refrigerator"):
        await call.answer("Неизвестный тип", show_alert=True)
        return
    await state.update_data(type=vtype)
    await state.set_state(AddVehicle.waiting_for_fuel_norm)
    await call.message.edit_text(msg.ADD_VEHICLE_FUEL_NORM)
    await call.answer()


@owner_router.message(AddVehicle.waiting_for_fuel_norm)
async def add_vehicle_fuel(message: Message, state: FSMContext, session: AsyncSession) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        norm = Decimal(raw)
        if norm <= 0 or norm > 100:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer(msg.ADD_VEHICLE_INVALID_NORM)
        return

    owner = await _get_owner(session, message.from_user.id)
    if owner is None:
        await message.answer(msg.SOMETHING_WRONG)
        await state.clear()
        return

    data = await state.get_data()
    vehicle = Vehicle(
        owner_id=owner.id,
        license_plate=data["license_plate"],
        brand=data["brand"],
        type=data["type"],
        fuel_norm_per_100km=norm,
    )
    session.add(vehicle)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await message.answer(msg.ADD_VEHICLE_PLATE_EXISTS)
        await state.clear()
        return

    await state.clear()
    await message.answer(msg.ADD_VEHICLE_DONE.format(plate=data["license_plate"]))
    owner_refreshed = await _get_owner(session, message.from_user.id)
    if owner_refreshed is not None:
        await _show_main_menu(message, owner_refreshed)


# =========================================================================
# Одобрение / отклонение расхода (callback из уведомления Этапа 2)
# =========================================================================
async def _decide_expense(call: CallbackQuery, session: AsyncSession, driver_bot: Bot, approve: bool) -> None:
    try:
        expense_id = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        await call.answer("Некорректный запрос", show_alert=True)
        return

    owner = await _get_owner(session, call.from_user.id)
    if owner is None:
        await call.answer("Сначала /start", show_alert=True)
        return

    expense = await expense_service.decide_expense(
        session, expense_id=expense_id, approve=approve
    )
    if expense is None:
        await call.answer("Расход не найден", show_alert=True)
        return
    if expense.owner_id != owner.id:
        # чужой расход — не позволим решать
        await call.answer("Доступ запрещён", show_alert=True)
        return

    await log_event(
        session,
        owner_id=owner.id,
        driver_id=expense.driver_id,
        shift_id=expense.shift_id,
        trip_id=expense.trip_id,
        event_type="expense_approved" if approve else "expense_rejected",
        payload={"expense_id": expense.id, "amount": str(expense.amount_rub)},
    )
    await session.commit()

    # Убираем inline-кнопки и добавляем итог в caption/text
    decision = "✅ Одобрено" if expense.status == "approved" else "❌ Отклонено"
    category_label = expense_service.CATEGORY_LABELS.get(expense.category, expense.category)
    summary_suffix = f"\n\n<b>{decision}</b>"
    try:
        if call.message.caption is not None:
            await call.message.edit_caption(
                caption=call.message.caption + summary_suffix, reply_markup=None
            )
        else:
            await call.message.edit_text(
                text=(call.message.text or "") + summary_suffix, reply_markup=None
            )
    except Exception as exc:  # noqa: BLE001 — telegram капризен с edit, не валим процесс
        logger.warning("Failed to edit expense decision message: %s", exc)

    # Уведомить водителя
    driver = await session.get(Driver, expense.driver_id)
    if driver is not None and driver.telegram_id is not None:
        template = msg.EXPENSE_APPROVED_DRIVER if approve else msg.EXPENSE_REJECTED_DRIVER
        await notify_driver(
            driver_bot, session, driver.telegram_id,
            template.format(category=category_label, amount=expense.amount_rub),
        )

    await call.answer("Готово")


@owner_router.callback_query(F.data.startswith("expense:approve:"))
async def cb_expense_approve(call: CallbackQuery, session: AsyncSession, driver_bot: Bot) -> None:
    await _decide_expense(call, session, driver_bot, approve=True)


@owner_router.callback_query(F.data.startswith("expense:reject:"))
async def cb_expense_reject(call: CallbackQuery, session: AsyncSession, driver_bot: Bot) -> None:
    await _decide_expense(call, session, driver_bot, approve=False)


# =========================================================================
# Заглушки для будущих действий + fallback
# =========================================================================
@owner_router.callback_query(F.data.startswith("driver:view:"))
async def cb_driver_view(call: CallbackQuery) -> None:
    await call.answer("Профиль водителя появится на Этапе 2", show_alert=True)


@owner_router.callback_query(F.data.startswith("vehicle:view:"))
async def cb_vehicle_view(call: CallbackQuery) -> None:
    await call.answer("Профиль машины появится на Этапе 2", show_alert=True)


@owner_router.message(Command("login"), StateFilter(any_state))
async def cmd_login(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Сгенерировать одноразовый код для входа в веб-кабинет."""
    await state.clear()
    owner = await _get_owner(session, message.from_user.id)
    if owner is None:
        await message.answer("Сначала /start — нужно зарегистрироваться.")
        return
    code = auth_service.issue_code(message.from_user.id)
    await message.answer(
        msg.OWNER_LOGIN_CODE.format(code=code, telegram_id=message.from_user.id)
    )


@owner_router.message(Command("cancel"), StateFilter(any_state))
async def cmd_cancel(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    owner = await _get_owner(session, message.from_user.id)
    if owner is not None:
        await _show_main_menu(message, owner)
    else:
        await message.answer("Отменено. /start чтобы начать заново.")


@owner_router.message()
async def fallback(message: Message) -> None:
    await message.answer(msg.UNKNOWN_COMMAND)
