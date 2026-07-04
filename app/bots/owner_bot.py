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
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import keyboards as kb
from app.bots import messages as msg
from app.bots.keyboards import (
    back_to_menu_keyboard,
    driver_salary_type_keyboard,
    drivers_list_keyboard,
    owner_main_menu,
    routes_list_keyboard,
    route_view_keyboard,
    vehicle_type_keyboard,
    vehicles_list_keyboard,
)
from app.bots.notifications import notify_driver
from app.bots.states import (
    AddDriver,
    AddRouteTemplate,
    AddVehicle,
    EditExpenseAmount,
    Onboarding,
    OwnerRegistration,
    SetOdometer,
    SetTripRevenue,
    TripCalc,
    WipeAll,
)
from app.config import settings
from app.models import (
    Admin, Driver, Expense, ManualEntry, Owner, RouteTemplate, Shift, Subscription, Trip, Vehicle,
)
from app.services import (
    auth_service,
    billing,
    expense_service,
    maintenance_service,
    salary_service,
    telemetry_service,
    trip_service,
)
from app.services.cash_pending import PENDING as CASH_PENDING
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

    # Админ чьего-то кабинета? Не регистрируем как нового владельца —
    # направляем на вход через /login.
    admin = (
        await session.execute(
            select(Admin).where(Admin.telegram_id == message.from_user.id)
        )
    ).scalar_one_or_none()
    if admin is not None:
        await message.answer(
            "Вы добавлены администратором кабинета. Для входа отправьте /login "
            "и введите код на странице входа."
        )
        return

    # новый владелец → 5-шаговый онбординг
    await state.set_state(Onboarding.company)
    await message.answer(
        "🚛 <b>Добро пожаловать в Автопарк TMS!</b>\n\n"
        "Помогу настроить за 5 минут. После этого сразу сможете "
        "открыть первую смену.\n\n"
        "<b>Шаг 1/5.</b> Как называется ваша компания или ИП?\n"
        "<i>Например: ИП Иванов</i>"
    )


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
# Онбординг — 5 шагов
# =========================================================================
@owner_router.message(Onboarding.company)
async def onb_company(message: Message, state: FSMContext) -> None:
    company = (message.text or "").strip()
    if len(company) < 2:
        await message.answer("Слишком короткое название. Попробуйте ещё раз.")
        return
    await state.update_data(company=company)
    await state.set_state(Onboarding.phone)
    await message.answer(
        f"✅ <b>{company}</b>.\n\n"
        f"<b>Шаг 2/5.</b> Ваш телефон в формате +7XXXXXXXXXX:"
    )


@owner_router.message(Onboarding.phone)
async def onb_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if not PHONE_RE.match(phone):
        await message.answer(msg.OWNER_INVALID_PHONE)
        return
    await state.update_data(phone=phone)
    await state.set_state(Onboarding.vehicle_plate)
    await message.answer(
        "<b>Шаг 3/5.</b> Госномер вашей первой машины (например, А123БВ777):"
    )


@owner_router.message(Onboarding.vehicle_plate)
async def onb_vehicle_plate(message: Message, state: FSMContext) -> None:
    plate = (message.text or "").strip().upper().replace(" ", "")
    if len(plate) < 6:
        await message.answer("Госномер слишком короткий. Введите ещё раз.")
        return
    await state.update_data(vehicle_plate=plate)
    await state.set_state(Onboarding.vehicle_brand)
    await message.answer("Марка машины (например, ГАЗель Next):")


@owner_router.message(Onboarding.vehicle_brand)
async def onb_vehicle_brand(message: Message, state: FSMContext) -> None:
    brand = (message.text or "").strip()
    if not brand:
        await message.answer("Введите марку машины.")
        return
    await state.update_data(vehicle_brand=brand)
    await state.set_state(Onboarding.driver_name)
    await message.answer("<b>Шаг 4/5.</b> ФИО первого водителя:")


@owner_router.message(Onboarding.driver_name)
async def onb_driver_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Слишком коротко. Введите ФИО водителя.")
        return
    await state.update_data(driver_name=name)
    await state.set_state(Onboarding.driver_phone)
    await message.answer("Телефон водителя в формате +7XXXXXXXXXX:")


@owner_router.message(Onboarding.driver_phone)
async def onb_driver_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if not PHONE_RE.match(phone):
        await message.answer(msg.OWNER_INVALID_PHONE)
        return
    await state.update_data(driver_phone=phone)
    await state.set_state(Onboarding.route_from)
    await message.answer(
        "<b>Шаг 5/5.</b> Можно сразу создать первый шаблон маршрута — "
        "тогда водителю не придётся каждый раз вбивать города руками.\n\n"
        "Откуда (город) — или нажмите «Пропустить»:",
        reply_markup=kb.skip_or_cancel_inline("onb:skip_route"),
    )


@owner_router.callback_query(
    Onboarding.route_from, F.data == "onb:skip_route"
)
async def cb_onb_skip_route(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, driver_bot: Bot
) -> None:
    await call.message.delete()
    # сразу завершаем без шаблона
    await state.update_data(route_from=None)
    await _onboarding_complete(call.message, state, session, driver_bot, destination=None)
    await call.answer()


@owner_router.message(Onboarding.route_from)
async def onb_route_from(message: Message, state: FSMContext) -> None:
    origin = (message.text or "").strip()
    if not origin:
        await message.answer("Введите город или нажмите «Пропустить».")
        return
    await state.update_data(route_from=origin)
    await state.set_state(Onboarding.route_to)
    await message.answer("Куда:")


@owner_router.message(Onboarding.route_to)
async def onb_finalize(
    message: Message, state: FSMContext, session: AsyncSession, driver_bot: Bot
) -> None:
    destination = (message.text or "").strip()
    if not destination:
        await message.answer("Введите город назначения.")
        return
    await _onboarding_complete(message, state, session, driver_bot, destination=destination)


async def _onboarding_complete(
    reply_target: Message,
    state: FSMContext,
    session: AsyncSession,
    driver_bot: Bot,
    destination: str | None,
) -> None:
    data = await state.get_data()

    # 1. Owner
    owner = Owner(
        telegram_id=reply_target.chat.id,
        full_name=reply_target.chat.full_name,
        company_name=data["company"],
        phone=data["phone"],
    )
    session.add(owner)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await _get_owner(session, reply_target.chat.id)
        if existing is not None:
            await state.clear()
            await _show_main_menu(reply_target, existing)
            return
        logger.exception("Onboarding owner create failed")
        await state.clear()
        await reply_target.answer(msg.SOMETHING_WRONG)
        return

    # 2. Free subscription
    session.add(Subscription(
        owner_id=owner.id, plan="free",
        vehicles_limit=billing.PLANS["free"].vehicles_limit,
    ))

    # 3. Vehicle
    vehicle = Vehicle(
        owner_id=owner.id,
        license_plate=data["vehicle_plate"],
        brand=data["vehicle_brand"],
        type="truck",
        fuel_norm_per_100km=Decimal("12"),
    )
    session.add(vehicle)

    # 4. Driver с invite-токеном
    invite_token = uuid.uuid4().hex
    driver = Driver(
        owner_id=owner.id,
        full_name=data["driver_name"],
        phone=data["driver_phone"],
        salary_type="per_km",
        salary_rate=Decimal("8"),
        invite_token=invite_token,
    )
    session.add(driver)

    # 5. Route template — только если указали оба города
    route_line = ""
    if data.get("route_from") and destination:
        session.add(RouteTemplate(
            owner_id=owner.id,
            name=f"{data['route_from']} → {destination}",
            origin=data["route_from"],
            destination=destination,
        ))
        route_line = f"Маршрут: <b>{data['route_from']} → {destination}</b>\n"

    await session.commit()
    await state.clear()

    me = await driver_bot.get_me()
    link = f"https://t.me/{me.username}?start={invite_token}"
    await reply_target.answer(
        "🎉 <b>Готово!</b>\n\n"
        f"Компания: <b>{owner.company_name}</b>\n"
        f"Машина: <b>{vehicle.license_plate}</b> ({vehicle.brand})\n"
        f"Водитель: <b>{driver.full_name}</b>\n"
        f"{route_line}"
        f"Тариф: <b>FREE</b> (до 2 машин)\n\n"
        "Отправьте водителю ссылку для подключения:\n"
        f"<code>{link}</code>\n\n"
        "Дальше: /tariffs · /calc · /login (вход в веб-кабинет)"
    )
    await _show_main_menu(reply_target, owner)


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
# Часовой пояс владельца (баг E2) — иначе всё показывается по Москве
# =========================================================================
async def _show_timezone_picker(target: Message, owner: Owner) -> None:
    await target.answer(
        f"Ваш часовой пояс: <b>{owner.timezone}</b>.\nВыберите свой регион:",
        reply_markup=kb.timezone_keyboard(),
    )


@owner_router.message(Command("timezone"), StateFilter(any_state))
async def cmd_timezone(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    owner = await _get_owner(session, message.from_user.id)
    if owner is None:
        await message.answer("Сначала /start")
        return
    await _show_timezone_picker(message, owner)


@owner_router.callback_query(F.data == "owner:timezone")
async def cb_owner_timezone(call: CallbackQuery, session: AsyncSession) -> None:
    owner = await _get_owner(session, call.from_user.id)
    if owner is None:
        await call.answer("Сначала /start", show_alert=True)
        return
    await call.message.edit_text(
        f"Ваш часовой пояс: <b>{owner.timezone}</b>.\nВыберите свой регион:",
        reply_markup=kb.timezone_keyboard(),
    )
    await call.answer()


@owner_router.callback_query(F.data.startswith("tz:set:"))
async def cb_set_timezone(call: CallbackQuery, session: AsyncSession) -> None:
    owner = await _get_owner(session, call.from_user.id)
    if owner is None:
        await call.answer("Сначала /start", show_alert=True)
        return
    try:
        idx = int(call.data.split(":")[2])
        tz_name = kb.RU_TIMEZONES[idx][0]
    except (IndexError, ValueError):
        await call.answer("Некорректный выбор", show_alert=True)
        return
    owner.timezone = tz_name
    await session.commit()
    await call.message.edit_text(
        f"✅ Часовой пояс установлен: <b>{tz_name}</b>.\n"
        "Время в боте и кабинете теперь по вашему региону.",
        reply_markup=back_to_menu_keyboard(),
    )
    await call.answer("Готово")


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
async def cb_add_driver(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    # Проверяем владельца СРАЗУ, а не после ввода всех данных: если аккаунт не
    # зарегистрирован владельцем (напр. тестовый/второй), даём понятное сообщение.
    owner = await _get_owner(session, call.from_user.id)
    if owner is None:
        await call.answer("Сначала пройдите регистрацию владельца: /start", show_alert=True)
        return
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
    if salary_type not in ("per_km", "per_trip", "percent", "fixed_per_shift", "fixed_per_month"):
        await call.answer("Неизвестный тип", show_alert=True)
        return
    await state.update_data(salary_type=salary_type)
    await state.set_state(AddDriver.waiting_for_salary_rate)

    prompt = {
        "per_km": msg.ADD_DRIVER_RATE_PER_KM,
        "per_trip": msg.ADD_DRIVER_RATE_PER_TRIP,
        "percent": msg.ADD_DRIVER_RATE_PERCENT,
        "fixed_per_shift": msg.ADD_DRIVER_RATE_FIXED,
        "fixed_per_month": "Введите оклад водителя за месяц (₽):",
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

    await state.update_data(salary_rate=str(rate))
    await state.set_state(AddDriver.waiting_for_shift_start)
    await message.answer(
        msg.ADD_DRIVER_SHIFT_START,
        reply_markup=kb.skip_or_cancel_inline("driver:skip_shift_time"),
    )


_SHIFT_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


async def _finalize_driver(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    driver_bot: Bot,
    shift_start_time: str | None,
) -> None:
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
        salary_rate=Decimal(data["salary_rate"]),
        invite_token=invite_token,
        shift_start_time=shift_start_time,
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


@owner_router.message(AddDriver.waiting_for_shift_start)
async def add_driver_shift_start(
    message: Message, state: FSMContext, session: AsyncSession, driver_bot: Bot
) -> None:
    text = (message.text or "").strip()
    if not _SHIFT_TIME_RE.match(text):
        await message.answer(msg.ADD_DRIVER_SHIFT_TIME_INVALID)
        return
    await _finalize_driver(message, state, session, driver_bot, shift_start_time=text)


@owner_router.callback_query(
    AddDriver.waiting_for_shift_start, F.data == "driver:skip_shift_time"
)
async def cb_add_driver_skip_shift_time(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, driver_bot: Bot
) -> None:
    await call.message.delete()
    await _finalize_driver(call.message, state, session, driver_bot, shift_start_time=None)
    await call.answer()


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
async def cb_add_vehicle(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    owner = await _get_owner(session, call.from_user.id)
    if owner is None:
        await call.answer("Сначала /start", show_alert=True)
        return
    can_add, count, limit = await billing.can_add_vehicle(session, owner.id)
    if not can_add:
        await call.message.edit_text(
            f"⛔ На вашем тарифе максимум {limit} машин ({count}/{limit} занято).\n"
            f"Расширить: /tariffs",
            reply_markup=back_to_menu_keyboard(),
        )
        await call.answer()
        return
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
async def add_vehicle_fuel(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", ".")
    try:
        norm = Decimal(raw)
        if norm <= 0 or norm > 100:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer(msg.ADD_VEHICLE_INVALID_NORM)
        return

    await state.update_data(fuel_norm=str(norm))
    await state.set_state(AddVehicle.waiting_for_osago)
    await message.answer(
        msg.ADD_VEHICLE_OSAGO,
        reply_markup=kb.skip_or_cancel_inline("vehicle:skip_osago"),
    )


_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def _parse_dot_date(text: str):
    m = _DATE_RE.match(text.strip())
    if not m:
        return None
    from datetime import date
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


async def _advance_vehicle_doc(
    message_or_call,
    state: FSMContext,
    next_state,
    next_prompt: str,
    skip_callback: str,
) -> None:
    """Сжатый helper: переход на следующий шаг ввода дат документов."""
    await state.set_state(next_state)
    target = message_or_call if isinstance(message_or_call, Message) else message_or_call
    await target.answer(
        next_prompt,
        reply_markup=kb.skip_or_cancel_inline(skip_callback),
    )


@owner_router.message(AddVehicle.waiting_for_osago)
async def add_vehicle_osago(message: Message, state: FSMContext) -> None:
    parsed = _parse_dot_date(message.text or "")
    if parsed is None:
        await message.answer(msg.ADD_VEHICLE_DATE_INVALID)
        return
    await state.update_data(osago=parsed.isoformat())
    await _advance_vehicle_doc(
        message, state, AddVehicle.waiting_for_inspection,
        msg.ADD_VEHICLE_INSPECTION, "vehicle:skip_inspection",
    )


@owner_router.callback_query(AddVehicle.waiting_for_osago, F.data == "vehicle:skip_osago")
async def cb_add_vehicle_skip_osago(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.delete()
    await _advance_vehicle_doc(
        call.message, state, AddVehicle.waiting_for_inspection,
        msg.ADD_VEHICLE_INSPECTION, "vehicle:skip_inspection",
    )
    await call.answer()


@owner_router.message(AddVehicle.waiting_for_inspection)
async def add_vehicle_inspection(message: Message, state: FSMContext) -> None:
    parsed = _parse_dot_date(message.text or "")
    if parsed is None:
        await message.answer(msg.ADD_VEHICLE_DATE_INVALID)
        return
    await state.update_data(inspection=parsed.isoformat())
    await _advance_vehicle_doc(
        message, state, AddVehicle.waiting_for_tacho,
        msg.ADD_VEHICLE_TACHO, "vehicle:skip_tacho",
    )


@owner_router.callback_query(
    AddVehicle.waiting_for_inspection, F.data == "vehicle:skip_inspection"
)
async def cb_add_vehicle_skip_inspection(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.delete()
    await _advance_vehicle_doc(
        call.message, state, AddVehicle.waiting_for_tacho,
        msg.ADD_VEHICLE_TACHO, "vehicle:skip_tacho",
    )
    await call.answer()


async def _finalize_vehicle(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    tacho_date: str | None,
    user_id: int,
) -> None:
    # ВАЖНО: при вызове из callback `message` — это сообщение БОТА, у него
    # message.from_user это бот. Поэтому владельца ищем по явному user_id
    # (call.from_user.id), иначе owner=None → «Что-то пошло не так» (был баг E1).
    owner = await _get_owner(session, user_id)
    if owner is None:
        await message.answer(msg.SOMETHING_WRONG)
        await state.clear()
        return

    from datetime import date as _date
    data = await state.get_data()
    vehicle = Vehicle(
        owner_id=owner.id,
        license_plate=data["license_plate"],
        brand=data["brand"],
        type=data["type"],
        fuel_norm_per_100km=Decimal(data["fuel_norm"]),
        osago_expires=_date.fromisoformat(data["osago"]) if data.get("osago") else None,
        inspection_expires=_date.fromisoformat(data["inspection"]) if data.get("inspection") else None,
        tacho_expires=_date.fromisoformat(tacho_date) if tacho_date else None,
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
    owner_refreshed = await _get_owner(session, user_id)
    if owner_refreshed is not None:
        await _show_main_menu(message, owner_refreshed)


@owner_router.message(AddVehicle.waiting_for_tacho)
async def add_vehicle_tacho(message: Message, state: FSMContext, session: AsyncSession) -> None:
    parsed = _parse_dot_date(message.text or "")
    if parsed is None:
        await message.answer(msg.ADD_VEHICLE_DATE_INVALID)
        return
    await _finalize_vehicle(
        message, state, session, tacho_date=parsed.isoformat(),
        user_id=message.from_user.id,
    )


@owner_router.callback_query(AddVehicle.waiting_for_tacho, F.data == "vehicle:skip_tacho")
async def cb_add_vehicle_skip_tacho(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    await call.message.delete()
    await _finalize_vehicle(
        call.message, state, session, tacho_date=None, user_id=call.from_user.id,
    )
    await call.answer()


# =========================================================================
# Шаблоны маршрутов
# =========================================================================
@owner_router.callback_query(F.data == "owner:routes")
async def cb_routes(call: CallbackQuery, session: AsyncSession) -> None:
    owner = await _get_owner(session, call.from_user.id)
    if owner is None:
        await call.answer("Сначала /start", show_alert=True)
        return
    res = await session.execute(
        select(RouteTemplate)
        .where(RouteTemplate.owner_id == owner.id, RouteTemplate.is_active.is_(True))
        .order_by(RouteTemplate.name)
    )
    templates = list(res.scalars().all())
    header = msg.ROUTES_LIST_HEADER if templates else msg.ROUTES_EMPTY
    if templates:
        lines = [msg.ROUTES_LIST_HEADER, ""]
        for t in templates:
            lines.append(f"• <b>{t.name}</b> — {t.origin} → {t.destination}")
        header = "\n".join(lines)
    await call.message.edit_text(header, reply_markup=routes_list_keyboard(templates))
    await call.answer()


@owner_router.callback_query(F.data == "route:add")
async def cb_route_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddRouteTemplate.waiting_for_name)
    await call.message.edit_text(msg.ROUTE_ADD_NAME)
    await call.answer()


@owner_router.message(AddRouteTemplate.waiting_for_name)
async def route_add_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer(msg.ROUTE_ADD_NAME)
        return
    await state.update_data(name=name)
    await state.set_state(AddRouteTemplate.waiting_for_origin)
    await message.answer(msg.ROUTE_ADD_ORIGIN)


@owner_router.message(AddRouteTemplate.waiting_for_origin)
async def route_add_origin(message: Message, state: FSMContext) -> None:
    origin = (message.text or "").strip()
    if not origin:
        await message.answer(msg.ROUTE_ADD_ORIGIN)
        return
    await state.update_data(origin=origin)
    await state.set_state(AddRouteTemplate.waiting_for_destination)
    await message.answer(msg.ROUTE_ADD_DESTINATION)


@owner_router.message(AddRouteTemplate.waiting_for_destination)
async def route_add_destination(message: Message, state: FSMContext) -> None:
    destination = (message.text or "").strip()
    if not destination:
        await message.answer(msg.ROUTE_ADD_DESTINATION)
        return
    await state.update_data(destination=destination)
    await state.set_state(AddRouteTemplate.waiting_for_cargo)
    await message.answer(
        msg.ROUTE_ADD_CARGO, reply_markup=kb.skip_or_cancel_inline("route:skip_cargo")
    )


async def _finalize_route(
    reply_target: Message, state: FSMContext, session: AsyncSession, cargo: str | None
) -> None:
    owner = await _get_owner(session, reply_target.chat.id) if isinstance(reply_target, Message) else None
    # reply_target.chat.id может относиться не к owner-у; используем from_user был выше — здесь
    # надёжнее: возьмём owner из state.update_data (мы сохранили его id неявно — нет).
    # Поэтому ищем через update.from_user — но это callback/message. Берём из data.
    # На самом деле в state у нас нет owner_id. Возьмём через chat:
    # для owner-бота private chat == owner.telegram_id, что мы и используем в _get_owner.
    if owner is None:
        await reply_target.answer(msg.SOMETHING_WRONG)
        await state.clear()
        return
    data = await state.get_data()
    template = RouteTemplate(
        owner_id=owner.id,
        name=data["name"], origin=data["origin"], destination=data["destination"],
        default_cargo=cargo,
    )
    session.add(template)
    await session.commit()
    await state.clear()
    await reply_target.answer(msg.ROUTE_SAVED)
    await _show_main_menu(reply_target, owner)


@owner_router.message(AddRouteTemplate.waiting_for_cargo)
async def route_add_cargo(message: Message, state: FSMContext, session: AsyncSession) -> None:
    cargo = (message.text or "").strip() or None
    await _finalize_route(message, state, session, cargo)


@owner_router.callback_query(
    AddRouteTemplate.waiting_for_cargo, F.data == "route:skip_cargo"
)
async def cb_route_skip_cargo(
    call: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    await call.message.delete()
    await _finalize_route(call.message, state, session, cargo=None)
    await call.answer()


@owner_router.callback_query(F.data.startswith("route:view:"))
async def cb_route_view(call: CallbackQuery, session: AsyncSession) -> None:
    template_id = int(call.data.split(":")[2])
    template = await session.get(RouteTemplate, template_id)
    owner = await _get_owner(session, call.from_user.id)
    if template is None or owner is None or template.owner_id != owner.id:
        await call.answer("Шаблон не найден", show_alert=True)
        return
    await call.message.edit_text(
        msg.ROUTE_VIEW.format(
            name=template.name, origin=template.origin,
            destination=template.destination,
            cargo=template.default_cargo or "—",
        ),
        reply_markup=route_view_keyboard(template.id),
    )
    await call.answer()


@owner_router.callback_query(F.data.startswith("route:del:"))
async def cb_route_delete(call: CallbackQuery, session: AsyncSession) -> None:
    template_id = int(call.data.split(":")[2])
    template = await session.get(RouteTemplate, template_id)
    owner = await _get_owner(session, call.from_user.id)
    if template is None or owner is None or template.owner_id != owner.id:
        await call.answer("Шаблон не найден", show_alert=True)
        return
    template.is_active = False
    await session.commit()
    await call.answer(msg.ROUTE_DELETED)
    # вернуться к списку
    res = await session.execute(
        select(RouteTemplate)
        .where(RouteTemplate.owner_id == owner.id, RouteTemplate.is_active.is_(True))
        .order_by(RouteTemplate.name)
    )
    templates = list(res.scalars().all())
    header = msg.ROUTES_LIST_HEADER if templates else msg.ROUTES_EMPTY
    if templates:
        lines = [msg.ROUTES_LIST_HEADER, ""]
        for t in templates:
            lines.append(f"• <b>{t.name}</b> — {t.origin} → {t.destination}")
        header = "\n".join(lines)
    await call.message.edit_text(header, reply_markup=routes_list_keyboard(templates))


# =========================================================================
# Указать выручку рейса (callback из уведомления о завершении)
# =========================================================================
@owner_router.callback_query(F.data.startswith("trip:revenue:"))
async def cb_trip_revenue_start(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    try:
        trip_id = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        await call.answer("Некорректный запрос", show_alert=True)
        return
    # сразу убираем кнопку у сообщения, чтобы повторно не нажимали
    # (иначе при втором клике запускается параллельный FSM и владелец вводит выручку дважды).
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 — telegram капризен с edit, не валим callback
        pass
    trip = await session.get(Trip, trip_id)
    await state.set_state(SetTripRevenue.waiting_for_amount)
    await state.update_data(trip_id=trip_id)
    await call.answer()
    # Владелец — главный: финальную выручку он может указать/переписать.
    # Если водитель предложил сумму, показываем её как черновик.
    if trip is not None and trip.revenue_rub is not None:
        await call.message.answer(
            f"Текущая выручка: {trip.revenue_rub:.0f} ₽. Введите новую сумму:"
        )
    elif trip is not None and trip.driver_revenue_pending_rub is not None:
        await call.message.answer(
            f"Водитель предложил: {trip.driver_revenue_pending_rub:.0f} ₽. "
            "Введите финальную сумму:"
        )
    else:
        await call.message.answer("Введите выручку по рейсу в рублях:")


@owner_router.callback_query(F.data.startswith("trev:ok:"))
async def cb_trip_revenue_approve(call: CallbackQuery, session: AsyncSession) -> None:
    """Владелец одобряет выручку, указанную водителем."""
    try:
        trip_id = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        await call.answer("Некорректный запрос", show_alert=True)
        return
    owner = await _get_owner(session, call.from_user.id)
    trip = await session.get(Trip, trip_id)
    if owner is None or trip is None or trip.owner_id != owner.id:
        await call.answer("Рейс не найден", show_alert=True)
        return
    approved = await trip_service.approve_trip_driver_revenue(session, trip=trip)
    if approved:
        await log_event(
            session, owner_id=owner.id, driver_id=trip.driver_id,
            shift_id=trip.shift_id, trip_id=trip.id,
            event_type="trip_revenue_approved", payload={"revenue": str(trip.revenue_rub)},
        )
        await session.commit()
    elif trip.revenue_rub is None:
        await call.answer("Нет суммы на подтверждении", show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await call.message.answer(
        msg.NOTIFY_TRIP_REVENUE_APPROVED.format(amount=f"{(trip.revenue_rub or 0):.0f}")
    )
    await call.answer("Готово")


@owner_router.callback_query(F.data.startswith("trev:edit:"))
async def cb_trip_revenue_edit(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Владелец хочет изменить сумму, указанную водителем."""
    try:
        trip_id = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        await call.answer("Некорректный запрос", show_alert=True)
        return
    owner = await _get_owner(session, call.from_user.id)
    trip = await session.get(Trip, trip_id)
    if owner is None or trip is None or trip.owner_id != owner.id:
        await call.answer("Рейс не найден", show_alert=True)
        return
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await state.set_state(SetTripRevenue.waiting_for_amount)
    await state.update_data(trip_id=trip_id)
    await call.answer()
    if trip.driver_revenue_pending_rub is not None:
        await call.message.answer(
            f"Водитель предложил: {trip.driver_revenue_pending_rub:.0f} ₽. "
            "Введите правильную выручку по рейсу в рублях:"
        )
    else:
        await call.message.answer("Введите правильную выручку по рейсу в рублях:")


@owner_router.message(SetTripRevenue.waiting_for_amount)
async def set_trip_revenue(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    raw = (message.text or "").strip().replace(",", ".").replace(" ", "")
    try:
        revenue = Decimal(raw)
        if revenue < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer(msg.TRIP_REVENUE_INVALID)
        return

    data = await state.get_data()
    trip = await session.get(Trip, data["trip_id"])
    owner = await _get_owner(session, message.from_user.id)
    if trip is None or owner is None or trip.owner_id != owner.id:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return

    await trip_service.set_trip_revenue(session, trip=trip, revenue_rub=revenue)
    await log_event(
        session, owner_id=owner.id, driver_id=trip.driver_id,
        shift_id=trip.shift_id, trip_id=trip.id,
        event_type="trip_revenue_set", payload={"revenue": str(revenue)},
    )
    await session.flush()
    await session.refresh(trip)

    # Карточка рентабельности рейса. ЗП водителя — оценка (точно посчитать
    # можно только в конце смены, когда известен пробег и число рейсов).
    fuel = Decimal(trip.fuel_cost_rub or 0)
    driver_obj = await session.get(Driver, trip.driver_id)
    shift = await session.get(Shift, trip.shift_id)
    completed_trips_in_shift = await session.execute(
        select(func.count(Trip.id)).where(
            Trip.shift_id == trip.shift_id, Trip.status == "completed"
        )
    )
    trips_count = completed_trips_in_shift.scalar_one() or 1
    salary = (
        salary_service.estimate_trip_salary(
            driver_obj, trip,
            shift_distance_km=shift.distance_km if shift else None,
            shift_completed_trips=trips_count,
        )
        if driver_obj is not None else Decimal(0)
    )
    profit_estimate = revenue - fuel - salary
    margin_pct = (profit_estimate / revenue * Decimal(100)) if revenue > 0 else Decimal(0)
    await session.commit()
    await state.clear()

    await message.answer(
        msg.TRIP_PNL_CARD.format(
            origin=trip.origin or "—",
            destination=trip.destination or "—",
            revenue=f"{revenue:,.0f}".replace(",", " "),
            fuel=f"{fuel:,.0f}".replace(",", " "),
            salary=f"{salary:,.0f}".replace(",", " "),
            profit=f"{profit_estimate:,.0f}".replace(",", " "),
            margin=f"{margin_pct:.0f}",
        )
    )


# =========================================================================
# Cash callbacks — подтвердить/оспорить сдачу нала
# =========================================================================
@owner_router.callback_query(F.data.startswith("cash:ok:"))
async def cb_cash_ok(call: CallbackQuery, session: AsyncSession, driver_bot: Bot) -> None:
    await _decide_cash(call, session, driver_bot, ok=True)


@owner_router.callback_query(F.data.startswith("cash:bad:"))
async def cb_cash_bad(call: CallbackQuery, session: AsyncSession, driver_bot: Bot) -> None:
    await _decide_cash(call, session, driver_bot, ok=False)


async def _decide_cash(
    call: CallbackQuery, session: AsyncSession, driver_bot: Bot, ok: bool
) -> None:
    token = call.data.split(":")[2]
    info = CASH_PENDING.pop(token, None)
    if info is None:
        await call.answer("Запрос устарел", show_alert=True)
        return

    owner = await _get_owner(session, call.from_user.id)
    if owner is None or owner.id != info["owner_id"]:
        await call.answer("Доступ запрещён", show_alert=True)
        return

    driver = await session.get(Driver, info["driver_id"])
    amount = Decimal(info["amount"])

    if ok:
        from datetime import date as _date
        entry = ManualEntry(
            owner_id=owner.id,
            type="income",
            category="нал от водителя",
            amount_rub=amount,
            description=f"Сдал {driver.full_name if driver else ''}",
            entry_date=_date.today(),
        )
        session.add(entry)

    await log_event(
        session, owner_id=owner.id,
        driver_id=info["driver_id"],
        event_type="cash_confirmed" if ok else "cash_disputed",
        payload={"amount": str(amount)},
    )
    await session.commit()

    suffix = f"\n\n<b>{msg.CASH_CONFIRMED_OWNER if ok else msg.CASH_DISPUTED_OWNER}</b>"
    try:
        if call.message.caption is not None:
            await call.message.edit_caption(
                caption=call.message.caption + suffix, reply_markup=None
            )
        else:
            await call.message.edit_text(
                text=(call.message.text or "") + suffix, reply_markup=None
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to edit cash decision message: %s", exc)

    if driver is not None and driver.telegram_id is not None:
        template = msg.CASH_CONFIRMED_DRIVER if ok else msg.CASH_DISPUTED_DRIVER
        await notify_driver(
            driver_bot, session, driver.telegram_id,
            template.format(amount=f"{amount:.0f}"),
        )
    await call.answer()


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

    # Уведомить водителя — только если включён флаг (по умолчанию не отвлекаем).
    if settings.feature_notify_driver_approval:
        driver = await session.get(Driver, expense.driver_id)
        if driver is not None and driver.telegram_id is not None:
            template = msg.EXPENSE_APPROVED_DRIVER if approve else msg.EXPENSE_REJECTED_DRIVER
            await notify_driver(
                driver_bot, session, driver.telegram_id,
                template.format(category=category_label, amount=expense.amount_rub),
            )

    await call.answer("Готово")


# =========================================================================
# Изменение суммы расхода владельцем (Блок C) — на случай ошибки распознавания
# =========================================================================
@owner_router.callback_query(F.data.startswith("expense:edit:"))
async def cb_expense_edit(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    try:
        expense_id = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        await call.answer("Некорректный запрос", show_alert=True)
        return
    owner = await _get_owner(session, call.from_user.id)
    if owner is None:
        await call.answer("Сначала /start", show_alert=True)
        return
    expense = await session.get(Expense, expense_id)
    if expense is None or expense.owner_id != owner.id:
        await call.answer("Расход не найден", show_alert=True)
        return
    if expense.status != "pending":
        await call.answer("Решение по этому расходу уже принято", show_alert=True)
        return
    await state.set_state(EditExpenseAmount.waiting_for_amount)
    await state.update_data(expense_id=expense_id)
    await call.message.answer(msg.EXPENSE_EDIT_ASK_AMOUNT)
    await call.answer()


@owner_router.message(EditExpenseAmount.waiting_for_amount)
async def edit_expense_amount(call_message: Message, state: FSMContext, session: AsyncSession) -> None:
    raw = (call_message.text or "").strip().replace(",", ".").replace(" ", "")
    try:
        amount = Decimal(raw)
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        await call_message.answer(msg.EXPENSE_AMOUNT_INVALID)
        return

    data = await state.get_data()
    expense_id = data.get("expense_id")
    owner = await _get_owner(session, call_message.from_user.id)
    expense = await session.get(Expense, expense_id) if expense_id else None
    if owner is None or expense is None or expense.owner_id != owner.id:
        await state.clear()
        await call_message.answer(msg.SOMETHING_WRONG)
        return

    old_amount = expense.amount_rub
    expense.amount_rub = amount.quantize(Decimal("0.01"))
    await log_event(
        session, owner_id=owner.id, driver_id=expense.driver_id,
        shift_id=expense.shift_id, trip_id=expense.trip_id,
        event_type="expense_amount_edited",
        payload={"expense_id": expense.id, "old": str(old_amount), "new": str(expense.amount_rub)},
    )
    await session.commit()
    await state.clear()

    category_label = expense_service.CATEGORY_LABELS.get(expense.category, expense.category)
    await call_message.answer(
        msg.EXPENSE_EDIT_DONE.format(category=category_label, amount=f"{expense.amount_rub:.0f}"),
        reply_markup=kb.expense_decision_keyboard(expense.id),
    )


# =========================================================================
# Пробег по фото одометра — владелец вписывает число (Правка 1)
# =========================================================================
@owner_router.callback_query(F.data.startswith("odo:"))
async def cb_odo_set(call: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    parts = call.data.split(":")
    if len(parts) != 3 or parts[1] not in ("start", "end"):
        await call.answer("Некорректный запрос", show_alert=True)
        return
    which, shift_id = parts[1], int(parts[2])
    owner = await _get_owner(session, call.from_user.id)
    shift = await session.get(Shift, shift_id)
    if owner is None or shift is None or shift.owner_id != owner.id:
        await call.answer("Смена не найдена", show_alert=True)
        return
    # Одноразовость: если пробег уже вписан — не даём вписать снова (баг «сколько угодно»).
    already = shift.odometer_start if which == "start" else shift.odometer_end
    if already is not None:
        await call.answer(f"Пробег уже указан: {already} км", show_alert=True)
        return
    await state.set_state(SetOdometer.waiting_for_value)
    await state.update_data(
        shift_id=shift_id, which=which,
        odo_chat=call.message.chat.id, odo_msg=call.message.message_id,
    )
    await call.message.answer(msg.ODOMETER_OWNER_ASK_VALUE)
    await call.answer()


@owner_router.message(SetOdometer.waiting_for_value)
async def odo_value(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot
) -> None:
    raw = (message.text or "").strip().replace(" ", "")
    if not raw.isdigit() or len(raw) > 7:
        await message.answer(msg.ODOMETER_OWNER_INVALID)
        return
    value = int(raw)
    data = await state.get_data()
    shift_id, which = data.get("shift_id"), data.get("which")
    owner = await _get_owner(session, message.from_user.id)
    shift = await session.get(Shift, shift_id) if shift_id else None
    if owner is None or shift is None or shift.owner_id != owner.id:
        await state.clear()
        await message.answer(msg.SOMETHING_WRONG)
        return

    # Валидация конца смены относительно начала (защита от отрицательного пробега).
    if which == "end" and shift.odometer_start is not None:
        if value < shift.odometer_start:
            await message.answer(
                f"Конец ({value}) не может быть меньше начала ({shift.odometer_start}). "
                "Введите ещё раз."
            )
            return
        if value - shift.odometer_start > 5000:
            await message.answer(
                f"За смену не могло быть {value - shift.odometer_start} км — похоже на опечатку. "
                "Введите ещё раз."
            )
            return

    if which == "start":
        shift.odometer_start = value
    else:
        shift.odometer_end = value
    await session.commit()
    await session.refresh(shift)  # distance_km — computed-колонка, перечитываем
    await state.clear()

    # Убираем кнопку «Указать пробег» с фото — чтобы не нажимали повторно.
    if data.get("odo_chat") and data.get("odo_msg"):
        try:
            await bot.edit_message_reply_markup(
                chat_id=data["odo_chat"], message_id=data["odo_msg"], reply_markup=None
            )
        except Exception:  # noqa: BLE001 — telegram капризен с edit, не критично
            pass

    if which == "start":
        await message.answer(msg.ODOMETER_OWNER_DONE_START.format(km=value))
    else:
        # Пересчитываем зарплату с учётом известного пробега.
        driver = await session.get(Driver, shift.driver_id)
        trips_res = await session.execute(select(Trip).where(Trip.shift_id == shift.id))
        trips = list(trips_res.scalars().all())
        salary = salary_service.calculate_salary(driver, shift, trips) if driver else 0
        text = msg.ODOMETER_OWNER_DONE_END.format(
            km=value,
            distance=shift.distance_km if shift.distance_km is not None else "—",
            salary=f"{salary:.0f}",
        )
        # Контроль честности: пробег по одометру против счётчика GPS-трекера
        # за эту смену (если телематика по машине уже идёт).
        if shift.distance_km is not None and shift.started_at is not None:
            from datetime import datetime as _dt, timezone as _tz

            gps_km = await telemetry_service.gps_mileage_for_period(
                session,
                vehicle_id=shift.vehicle_id,
                start=shift.started_at,
                end=shift.ended_at or _dt.now(_tz.utc),
            )
            if gps_km is not None:
                text += "\n" + telemetry_service.format_mileage_comparison(
                    shift.distance_km, gps_km
                )
        await message.answer(text)


@owner_router.callback_query(F.data.startswith("expense:approve:"))
async def cb_expense_approve(call: CallbackQuery, session: AsyncSession, driver_bot: Bot) -> None:
    await _decide_expense(call, session, driver_bot, approve=True)


@owner_router.callback_query(F.data.startswith("expense:reject:"))
async def cb_expense_reject(call: CallbackQuery, session: AsyncSession, driver_bot: Bot) -> None:
    await _decide_expense(call, session, driver_bot, approve=False)


# =========================================================================
# /calc — калькулятор рейса (без записи в БД)
# =========================================================================
DEFAULT_FUEL_PRICE = Decimal("68")  # ₽/л


@owner_router.message(Command("calc"), StateFilter(any_state))
async def cmd_calc(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(TripCalc.waiting_for_distance)
    await message.answer(
        "🧮 <b>Калькулятор рейса</b>\n\n"
        "Введите расстояние в км (например, 250):"
    )


@owner_router.message(TripCalc.waiting_for_distance)
async def calc_distance(message: Message, state: FSMContext) -> None:
    val = _parse_dec(message.text)
    if val is None or val <= 0:
        await message.answer("Не похоже на число. Введите расстояние в км.")
        return
    await state.update_data(distance=str(val))
    await state.set_state(TripCalc.waiting_for_rate)
    await message.answer("Ставка за рейс в рублях (например, 15000):")


@owner_router.message(TripCalc.waiting_for_rate)
async def calc_rate(message: Message, state: FSMContext) -> None:
    val = _parse_dec(message.text)
    if val is None or val <= 0:
        await message.answer("Не похоже на сумму. Введите выручку рейса в ₽.")
        return
    await state.update_data(rate=str(val))
    await state.set_state(TripCalc.waiting_for_fuel_norm)
    await message.answer(
        "Расход машины л/100км (например, 12.5).\n"
        "Если не знаете — введите 12."
    )


@owner_router.message(TripCalc.waiting_for_fuel_norm)
async def calc_finalize(message: Message, state: FSMContext) -> None:
    norm = _parse_dec(message.text)
    if norm is None or norm <= 0:
        await message.answer("Не похоже на число. Введите расход л/100км.")
        return
    data = await state.get_data()
    distance = Decimal(data["distance"])
    rate = Decimal(data["rate"])
    liters = (norm * distance) / Decimal(100)
    fuel_cost = (liters * DEFAULT_FUEL_PRICE).quantize(Decimal("0.01"))
    # ЗП водителя — для калькулятора берём грубый средний случай 8 ₽/км
    salary = (distance * Decimal("8")).quantize(Decimal("0.01"))
    profit = rate - fuel_cost - salary
    margin_pct = (profit / rate * Decimal(100)).quantize(Decimal("0.1")) if rate > 0 else Decimal(0)
    await state.clear()
    await message.answer(
        f"🧮 <b>Результат расчёта</b>\n\n"
        f"Расстояние: <b>{distance:.0f}</b> км\n"
        f"Выручка: <b>{rate:.0f}</b> ₽\n"
        f"Топливо: ~<b>{fuel_cost:.0f}</b> ₽ ({liters:.1f} л при {norm} л/100км)\n"
        f"ЗП водителя (грубо 8 ₽/км): <b>{salary:.0f}</b> ₽\n\n"
        f"💰 Прибыль: <b>{profit:.0f}</b> ₽\n"
        f"📈 Маржа: <b>{margin_pct}%</b>\n\n"
        f"<i>Расчёт приблизительный. Топливо считаем по 68 ₽/л.</i>"
    )


def _parse_dec(text: str | None) -> Decimal | None:
    if text is None:
        return None
    try:
        return Decimal(text.strip().replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None


# =========================================================================
# Заглушки для будущих действий + fallback
# =========================================================================
@owner_router.callback_query(F.data.startswith("driver:view:"))
async def cb_driver_view(call: CallbackQuery) -> None:
    await call.answer("Профиль водителя появится на Этапе 2", show_alert=True)


@owner_router.callback_query(F.data.startswith("vehicle:view:"))
async def cb_vehicle_view(call: CallbackQuery) -> None:
    await call.answer("Профиль машины появится на Этапе 2", show_alert=True)


@owner_router.message(Command("help"), StateFilter(any_state))
async def cmd_help(message: Message) -> None:
    await message.answer(msg.OWNER_HELP)


@owner_router.message(Command("tariffs"), StateFilter(any_state))
async def cmd_tariffs(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Показать тарифы. Кнопка «Написать для подключения» открывает чат с автором."""
    await state.clear()
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    # URL чата владельца проекта — пока хардкод, поменять на свой
    builder.button(text="✉️ Написать для подключения", url="https://t.me/")
    await message.answer(billing.format_tariffs(), reply_markup=builder.as_markup())


@owner_router.message(Command("login"), StateFilter(any_state))
async def cmd_login(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Сгенерировать одноразовый код для входа в веб-кабинет.
    Работает для владельца и для добавленного им администратора."""
    await state.clear()
    tid = message.from_user.id
    owner = await _get_owner(session, tid)
    if owner is None:
        # Может это администратор чьего-то кабинета?
        admin = (
            await session.execute(select(Admin).where(Admin.telegram_id == tid))
        ).scalar_one_or_none()
        if admin is None:
            await message.answer("Сначала /start — нужно зарегистрироваться.")
            return
    code = auth_service.issue_code(tid)
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


@owner_router.callback_query()
async def fallback_callback(call: CallbackQuery) -> None:
    """
    Срабатывает на устаревшие inline-кнопки (например, после рестарта процесса
    FSM-состояние пропало). Без этого Telegram через 10с показывает
    дефолтный alert «Произошла ошибка».
    """
    await call.answer(
        "Эта кнопка устарела. Нажмите /start чтобы открыть меню заново.",
        show_alert=True,
    )


# =========================================================================
# /wipe — СКРЫТАЯ команда полного сброса тестовых данных (нет в /help).
# Удаляет все рабочие данные владельца; аккаунт, реквизиты, админы и
# устройства кабинета остаются. Требует точную фразу подтверждения.
# =========================================================================
@owner_router.message(Command("wipe"), StateFilter(any_state))
async def cmd_wipe(message: Message, state: FSMContext, session: AsyncSession) -> None:
    owner = await _get_owner(session, message.from_user.id)
    if owner is None:
        await message.answer(msg.SOMETHING_WRONG)
        return
    await state.clear()
    await state.set_state(WipeAll.waiting_for_confirm)
    await message.answer(
        "⚠️ <b>Полный сброс данных</b>\n\n"
        "Будут БЕЗВОЗВРАТНО удалены: рейсы, смены, водители, машины, расходы, "
        "документы, GPS-данные, справочник РЦ, заказчики, ручные записи и события.\n\n"
        "Останутся: ваш аккаунт, реквизиты Исполнителя, админы и входы в кабинет.\n\n"
        f"Чтобы подтвердить, отправьте точную фразу:\n<b>{maintenance_service.WIPE_CONFIRM_PHRASE}</b>\n\n"
        "Отменить — /cancel или любой другой текст."
    )


@owner_router.message(WipeAll.waiting_for_confirm)
async def wipe_confirm(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    owner = await _get_owner(session, message.from_user.id)
    if owner is None:
        await message.answer(msg.SOMETHING_WRONG)
        return
    if (message.text or "").strip() != maintenance_service.WIPE_CONFIRM_PHRASE:
        await message.answer("Фраза не совпала — сброс отменён. Данные не тронуты.")
        await _show_main_menu(message, owner)
        return
    counts = await maintenance_service.wipe_owner_data(session, owner.id)
    await session.commit()
    if counts:
        lines = "\n".join(f"• {name}: {n}" for name, n in counts.items())
        await message.answer(
            f"🧹 Готово — тестовые данные стёрты:\n{lines}\n\n"
            "Кабинет чистый, можно начинать новую стадию теста. /start"
        )
    else:
        await message.answer("Удалять было нечего — кабинет уже пуст. /start")


@owner_router.message()
async def fallback(message: Message) -> None:
    await message.answer(msg.UNKNOWN_COMMAND)
