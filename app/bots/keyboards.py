"""
Клавиатуры.

Договорённость:
  - Владелец видит inline-кнопки (компактнее, удобно в кабинете).
  - Водитель видит reply-кнопки (большие, чтобы тыкать на трассе).

Reply-клавиатура водителя меняется под текущее состояние из БД —
у нас есть driver_keyboard_for_state(), которая возвращает правильный
набор кнопок.
"""
from aiogram.types import (
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from app.config import settings
from app.services.expense_service import CATEGORY_LABELS


# =========================================================================
# Тексты reply-кнопок водителя — собраны вместе, чтобы хендлеры могли
# матчить по точному совпадению.
# Короткие подписи, чтобы быстро жать с трассы.
# =========================================================================
BTN_START_SHIFT = "🟢 Начать смену"
BTN_END_SHIFT = "🔴 Конец смены"
BTN_NEW_TRIP = "🚛 Новый рейс"
BTN_TRIP_DEPART = "🚦 Выехал"
BTN_TRIP_UNLOADING = "📦 Выгрузка"
BTN_END_TRIP = "✅ Сдал груз"
BTN_UPLOAD_WAYBILL = "📄 Документ"
BTN_EXPENSE = "💳 Расход"
BTN_SOS = "🆘 SOS"
BTN_STATUS = "📋 Статус"
BTN_DOWNTIME = "⏸ Простой"
BTN_HANDED_CASH = "💵 Сдал деньги"
BTN_SKIP = "⏭ Пропустить"
BTN_SEND_LOCATION = "📍 Отправить геопозицию"
# Оффлайн-добавление задним числом (Блок D)
BTN_ADD_SHIFT = "➕ Добавить смену"
BTN_ADD_TRIP = "➕ Добавить рейс"


# =========================================================================
# ВЛАДЕЛЕЦ — главное меню
# =========================================================================
def owner_main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Мои водители", callback_data="owner:drivers")
    kb.button(text="🚚 Мои машины", callback_data="owner:vehicles")
    kb.button(text="🗺 Шаблоны маршрутов", callback_data="owner:routes")
    kb.button(text="📊 Статистика", callback_data="owner:stats")
    kb.button(text="🕒 Часовой пояс", callback_data="owner:timezone")
    kb.adjust(1)
    return kb.as_markup()


# Распространённые часовые пояса РФ (для установки владельцем, баг E2).
RU_TIMEZONES = [
    ("Europe/Kaliningrad", "Калининград (МСК−1)"),
    ("Europe/Moscow", "Москва (МСК)"),
    ("Europe/Samara", "Самара (МСК+1)"),
    ("Asia/Yekaterinburg", "Екатеринбург (МСК+2)"),
    ("Asia/Omsk", "Омск (МСК+3)"),
    ("Asia/Krasnoyarsk", "Красноярск (МСК+4)"),
    ("Asia/Irkutsk", "Иркутск (МСК+5)"),
    ("Asia/Yakutsk", "Якутск (МСК+6)"),
    ("Asia/Vladivostok", "Владивосток (МСК+7)"),
    ("Asia/Magadan", "Магадан (МСК+8)"),
    ("Asia/Kamchatka", "Камчатка (МСК+9)"),
]


def timezone_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, (_, label) in enumerate(RU_TIMEZONES):
        kb.button(text=label, callback_data=f"tz:set:{i}")
    kb.button(text="« Назад", callback_data="owner:menu")
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
    kb.button(text="Оклад за месяц", callback_data="salary:fixed_per_month")
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
def odometer_set_keyboard(shift_id: int, which: str) -> InlineKeyboardMarkup:
    """Кнопка владельцу под фото одометра: вписать пробег. which = 'start'|'end'."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📍 Указать пробег", callback_data=f"odo:{which}:{shift_id}")
    return kb.as_markup()


def expense_decision_keyboard(expense_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"expense:approve:{expense_id}")
    kb.button(text="✏️ Изменить", callback_data=f"expense:edit:{expense_id}")
    kb.button(text="❌ Отклонить", callback_data=f"expense:reject:{expense_id}")
    kb.adjust(3)
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
    rows: list[list[str]] = [[BTN_START_SHIFT]]
    # Расход доступен в любой момент, даже до открытия смены (Правка 3).
    rows.append([BTN_EXPENSE])
    # Оффлайн-добавление задним числом (когда не было связи на складе).
    rows.append([BTN_ADD_SHIFT, BTN_ADD_TRIP])
    # «Простой» и «Сдал деньги» — по флагам (по умолчанию скрыты).
    extras: list[str] = []
    if settings.feature_downtime:
        extras.append(BTN_DOWNTIME)
    if settings.feature_cash_handover:
        extras.append(BTN_HANDED_CASH)
    if extras:
        rows.append(extras)
    return _kb(*rows)


def manual_vehicle_keyboard(vehicles, prefix: str) -> InlineKeyboardMarkup:
    """Выбор машины для оффлайн-добавления. prefix: 'mshift' или 'mtrip'."""
    kb = InlineKeyboardBuilder()
    for v in vehicles:
        label = v.license_plate + (f" — {v.brand}" if v.brand else "")
        kb.button(text=label, callback_data=f"{prefix}:veh:{v.id}")
    kb.button(text="✖️ Отмена", callback_data=f"{prefix}:cancel")
    kb.adjust(1)
    return kb.as_markup()


def manual_route_keyboard(templates) -> InlineKeyboardMarkup:
    """Выбор маршрута для оффлайн-рейса: шаблон или вручную."""
    kb = InlineKeyboardBuilder()
    for t in templates:
        kb.button(text=f"🗺 {t.name}", callback_data=f"mtrip:rt:{t.id}")
    kb.button(text="✏️ Другой маршрут", callback_data="mtrip:manual")
    kb.button(text="✖️ Отмена", callback_data="mtrip:cancel")
    kb.adjust(1)
    return kb.as_markup()


def driver_shift_no_trip_kb() -> ReplyKeyboardMarkup:
    return _kb(
        [BTN_NEW_TRIP],
        [BTN_END_SHIFT],
        [BTN_EXPENSE, BTN_SOS],
    )


def driver_trip_created_kb() -> ReplyKeyboardMarkup:
    return _kb(
        [BTN_TRIP_DEPART],
        [BTN_UPLOAD_WAYBILL],
        [BTN_EXPENSE, BTN_SOS],
    )


def driver_trip_in_transit_kb() -> ReplyKeyboardMarkup:
    # При выключенных промежуточных статусах рейса (FEATURE_TRIP_STATUS_STEPS)
    # вместо «Выгрузка» сразу показываем «Сдал груз».
    main = BTN_TRIP_UNLOADING if settings.feature_trip_status_steps else BTN_END_TRIP
    return _kb(
        [main],
        [BTN_UPLOAD_WAYBILL],
        [BTN_EXPENSE, BTN_SOS],
    )


def driver_trip_unloading_kb() -> ReplyKeyboardMarkup:
    return _kb(
        [BTN_END_TRIP],
        [BTN_UPLOAD_WAYBILL],
        [BTN_EXPENSE, BTN_SOS],
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
# ВОДИТЕЛЬ — подтверждение «не обычной» машины (анти-миссклик)
# =========================================================================
def vehicle_confirm_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, беру эту", callback_data="shift:pickok")
    kb.button(text="↩️ Выбрать заново", callback_data="shift:repick")
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


# =========================================================================
# ВОДИТЕЛЬ — запрос геопозиции (reply, временно подменяет state-клавиатуру)
# =========================================================================
def location_request_keyboard() -> ReplyKeyboardMarkup:
    """
    request_location=True заставит Telegram запросить геопозицию
    при нажатии. «Пропустить» — обычная кнопка, дальше идём без координат.
    """
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text=BTN_SEND_LOCATION, request_location=True))
    builder.row(KeyboardButton(text=BTN_SKIP))
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


# =========================================================================
# ВОДИТЕЛЬ — выбор причины простоя
# =========================================================================
DOWNTIME_REASONS = {
    "breakdown": "🔧 Поломка",
    "no_orders": "📭 Нет заказов",
    "sick": "🤒 Болезнь",
    "other": "❓ Другое",
}


def downtime_reason_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for code, label in DOWNTIME_REASONS.items():
        kb.button(text=label, callback_data=f"dt:{code}")
    kb.button(text="✖️ Отмена", callback_data="dt:cancel")
    kb.adjust(1)
    return kb.as_markup()


# =========================================================================
# ВЛАДЕЛЕЦ — указать выручку рейса (inline под уведомлением о завершении)
# =========================================================================
def trip_revenue_keyboard(trip_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Указать выручку", callback_data=f"trip:revenue:{trip_id}")
    return kb.as_markup()


def driver_revenue_keyboard(trip_id: int) -> InlineKeyboardMarkup:
    """Кнопка водителю под завершённым рейсом — по желанию указать выручку."""
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Указать выручку", callback_data=f"drev:{trip_id}")
    return kb.as_markup()


def trip_revenue_decision_keyboard(trip_id: int) -> InlineKeyboardMarkup:
    """Владельцу: одобрить выручку от водителя или изменить сумму."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"trev:ok:{trip_id}")
    kb.button(text="✏️ Изменить", callback_data=f"trev:edit:{trip_id}")
    kb.adjust(2)
    return kb.as_markup()


# =========================================================================
# ВЛАДЕЛЕЦ — подтверждение что водитель сдал нал
# =========================================================================
def cash_decision_keyboard(entry_token: str) -> InlineKeyboardMarkup:
    """entry_token = uuid, по которому в FSM data найдём детали (так как сумма
    в callback_data не помещается осмысленно). Хранится во временном хранилище."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"cash:ok:{entry_token}")
    kb.button(text="❌ Оспорить", callback_data=f"cash:bad:{entry_token}")
    kb.adjust(2)
    return kb.as_markup()


# =========================================================================
# ВОДИТЕЛЬ — выбор маршрута при создании рейса
# =========================================================================
def route_template_keyboard(templates) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for t in templates:
        kb.button(text=f"🗺 {t.name}", callback_data=f"rt:pick:{t.id}")
    kb.button(text="✏️ Другой маршрут", callback_data="rt:manual")
    kb.button(text="✖️ Отмена", callback_data="rt:cancel")
    kb.adjust(1)
    return kb.as_markup()


def route_confirm_keyboard() -> InlineKeyboardMarkup:
    """После выбора шаблона: подтвердить или переснять выбор (баг E3 — водители
    часто жмут не тот шаблон)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать рейс", callback_data="rt:confirm")
    kb.button(text="✏️ Изменить маршрут", callback_data="rt:change")
    kb.button(text="✖️ Отмена", callback_data="rt:cancel")
    kb.adjust(1)
    return kb.as_markup()


# =========================================================================
# ВЛАДЕЛЕЦ — меню шаблонов маршрутов
# =========================================================================
def routes_list_keyboard(templates) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for t in templates:
        kb.button(text=f"🗺 {t.name}", callback_data=f"route:view:{t.id}")
    kb.button(text="➕ Добавить шаблон", callback_data="route:add")
    kb.button(text="« Назад", callback_data="owner:menu")
    kb.adjust(1)
    return kb.as_markup()


def route_view_keyboard(template_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить шаблон", callback_data=f"route:del:{template_id}")
    kb.button(text="« К списку", callback_data="owner:routes")
    kb.adjust(1)
    return kb.as_markup()


# =========================================================================
# Универсальные «пропустить» / «отмена»
# =========================================================================
def skip_or_cancel_inline(skip_data: str, cancel_data: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=BTN_SKIP, callback_data=skip_data)
    if cancel_data:
        kb.button(text="✖️ Отмена", callback_data=cancel_data)
    kb.adjust(1)
    return kb.as_markup()
