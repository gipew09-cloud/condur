"""
Клавиатуры.

Договорённость:
  - Владелец видит inline-кнопки (компактнее, удобно в кабинете).
  - Водитель видит reply-кнопки (большие, чтобы тыкать на трассе).

Reply-клавиатура водителя меняется под текущее состояние из БД —
у нас есть driver_keyboard_for_state(), которая возвращает правильный
набор кнопок.
"""
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.services.expense_service import CATEGORY_LABELS


# =========================================================================
# Тексты reply-кнопок водителя — собраны вместе, чтобы хендлеры могли
# матчить по точному совпадению.
# =========================================================================
BTN_START_SHIFT = "🚀 Начать смену"
BTN_END_SHIFT = "🏁 Завершить смену"
BTN_NEW_TRIP = "🛣 Новый рейс"
BTN_TRIP_DEPART = "🚛 Выехал"
BTN_TRIP_UNLOADING = "📦 На выгрузке"
BTN_END_TRIP = "✅ Завершить рейс"
BTN_UPLOAD_WAYBILL = "📄 Загрузить ТТН"
BTN_EXPENSE = "💸 Расход"
BTN_SOS = "🆘 SOS"
BTN_STATUS = "📋 Статус"
BTN_SKIP = "⏭ Пропустить фото"


# =========================================================================
# ВЛАДЕЛЕЦ — главное меню
# =========================================================================
def owner_main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Мои водители", callback_data="owner:drivers")
    kb.button(text="🚚 Мои машины", callback_data="owner:vehicles")
    kb.button(text="📊 Статистика", callback_data="owner:stats")
    kb.adjust(1)
    return kb.as_markup()


def drivers_list_keyboard(drivers, active_driver_ids: set[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for d in drivers:
        mark = "🟢" if d.id in active_driver_ids else "⚪️"
        kb.button(text=f"{mark} {d.full_name}", callback_data=f"driver:view:{d.id}")
    kb.button(text="➕ Добавить водителя", callback_data="driver:add")
    kb.button(text="« Назад", callback_data="owner:menu")
    kb.adjust(1)
    return kb.as_markup()


def driver_salary_type_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="За км", callback_data="salary:per_km")
    kb.button(text="За рейс", callback_data="salary:per_trip")
    kb.button(text="Процент с выручки", callback_data="salary:percent")
    kb.button(text="Фикс за смену", callback_data="salary:fixed_per_shift")
    kb.adjust(1)
    return kb.as_markup()


def vehicles_list_keyboard(vehicles, busy_vehicle_ids: set[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for v in vehicles:
        mark = "🟢" if v.id in busy_vehicle_ids else "⚪️"
        label = f"{mark} {v.license_plate}"
        if v.brand:
            label += f" — {v.brand}"
        kb.button(text=label, callback_data=f"vehicle:view:{v.id}")
    kb.button(text="➕ Добавить машину", callback_data="vehicle:add")
    kb.button(text="« Назад", callback_data="owner:menu")
    kb.adjust(1)
    return kb.as_markup()


def vehicle_type_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Грузовик", callback_data="vtype:truck")
    kb.button(text="Газель / фургон", callback_data="vtype:gazelle")
    kb.button(text="Рефрижератор", callback_data="vtype:refrigerator")
    kb.adjust(1)
    return kb.as_markup()


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="« Главное меню", callback_data="owner:menu")
    return kb.as_markup()


# =========================================================================
# ВЛАДЕЛЕЦ — одобрение расхода (inline под уведомлением)
# =========================================================================
def expense_decision_keyboard(expense_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"expense:approve:{expense_id}")
    kb.button(text="❌ Отклонить", callback_data=f"expense:reject:{expense_id}")
    kb.adjust(2)
    return kb.as_markup()


# =========================================================================
# ВОДИТЕЛЬ — reply-клавиатуры, разные под состояние
# =========================================================================
def _kb(*rows: list[str]) -> ReplyKeyboardMarkup:
    """Удобная обёртка: каждый row — список подписей кнопок одной строки."""
    builder = ReplyKeyboardBuilder()
    sizes: list[int] = []
    for row in rows:
        for text in row:
            builder.button(text=text)
        sizes.append(len(row))
    builder.adjust(*sizes)
    return builder.as_markup(resize_keyboard=True)


def driver_no_shift_kb() -> ReplyKeyboardMarkup:
    return _kb([BTN_START_SHIFT], [BTN_STATUS])


def driver_shift_no_trip_kb() -> ReplyKeyboardMarkup:
    return _kb(
        [BTN_NEW_TRIP],
        [BTN_END_SHIFT],
        [BTN_EXPENSE, BTN_SOS],
        [BTN_STATUS],
    )


def driver_trip_created_kb() -> ReplyKeyboardMarkup:
    return _kb(
        [BTN_TRIP_DEPART],
        [BTN_EXPENSE, BTN_SOS],
        [BTN_STATUS],
    )


def driver_trip_in_transit_kb() -> ReplyKeyboardMarkup:
    return _kb(
        [BTN_TRIP_UNLOADING],
        [BTN_UPLOAD_WAYBILL],
        [BTN_EXPENSE, BTN_SOS],
        [BTN_STATUS],
    )


def driver_trip_unloading_kb() -> ReplyKeyboardMarkup:
    return _kb(
        [BTN_END_TRIP],
        [BTN_UPLOAD_WAYBILL],
        [BTN_EXPENSE, BTN_SOS],
        [BTN_STATUS],
    )


def driver_keyboard_for_state(state: str) -> ReplyKeyboardMarkup:
    """
    Главный диспетчер: по строковому коду состояния возвращает нужный набор.
    Коды совпадают с тем, что возвращает driver_runtime_state() из хендлера.
    """
    return {
        "no_shift": driver_no_shift_kb(),
        "shift_no_trip": driver_shift_no_trip_kb(),
        "trip_created": driver_trip_created_kb(),
        "trip_in_transit": driver_trip_in_transit_kb(),
        "trip_unloading": driver_trip_unloading_kb(),
    }.get(state, driver_no_shift_kb())


def hide_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


# =========================================================================
# ВОДИТЕЛЬ — выбор машины при старте смены (inline)
# =========================================================================
def vehicle_pick_keyboard(vehicles) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for v in vehicles:
        label = v.license_plate + (f" — {v.brand}" if v.brand else "")
        kb.button(text=label, callback_data=f"shift:pick:{v.id}")
    kb.button(text="✖️ Отмена", callback_data="shift:cancel")
    kb.adjust(1)
    return kb.as_markup()


# =========================================================================
# ВОДИТЕЛЬ — выбор категории расхода (inline)
# =========================================================================
def expense_category_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, label in CATEGORY_LABELS.items():
        kb.button(text=label, callback_data=f"exp_cat:{code}")
    kb.button(text="✖️ Отмена", callback_data="exp_cat:cancel")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def expense_receipt_skip_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_SKIP, callback_data="exp_receipt:skip")
    return kb.as_markup()


# =========================================================================
# ВОДИТЕЛЬ — подтверждение SOS
# =========================================================================
def sos_confirm_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🆘 Да, нужна помощь", callback_data="sos:confirm")
    kb.button(text="Отмена", callback_data="sos:cancel")
    kb.adjust(1)
    return kb.as_markup()
