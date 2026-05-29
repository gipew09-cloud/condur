"""
Клавиатуры.

Договорённость:
  - Владелец видит inline-кнопки (компактнее, удобно в кабинете).
  - Водитель видит reply-кнопки (большие, чтобы тыкать на трассе).

На этом этапе у водителя ещё нет основного меню — он только регистрируется
по ссылке. Его reply-клавиатура появится на Этапе 2 (начать смену и т.д.).
"""
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ========== ВЛАДЕЛЕЦ — главное меню ==========
def owner_main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Мои водители", callback_data="owner:drivers")
    kb.button(text="🚚 Мои машины", callback_data="owner:vehicles")
    kb.button(text="📊 Статистика", callback_data="owner:stats")
    kb.adjust(1)
    return kb.as_markup()


# ========== ВЛАДЕЛЕЦ — список водителей ==========
def drivers_list_keyboard(drivers, active_driver_ids: set[int]) -> InlineKeyboardMarkup:
    """
    drivers — list[Driver].
    active_driver_ids — id водителей, у кого сейчас открыта смена.
    """
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
    kb.button(text="Процент с выручки", callback_data="salary:percent")
    kb.button(text="Фикс за смену", callback_data="salary:fixed_per_shift")
    kb.adjust(1)
    return kb.as_markup()


# ========== ВЛАДЕЛЕЦ — список машин ==========
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
