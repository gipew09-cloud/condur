"""
Все FSM-состояния ботов. StatesGroup = набор шагов в одном диалоге.

Важно: FSM — только подсказка для UI. Источник истины — БД.
В начале каждого хендлера сначала смотрим, что в БД, а не что в FSM.
"""
from aiogram.fsm.state import State, StatesGroup


# ========== ВЛАДЕЛЕЦ ==========
class OwnerRegistration(StatesGroup):
    """Первый /start у владельца — спрашиваем компанию и телефон."""
    waiting_for_company = State()
    waiting_for_phone = State()


class AddDriver(StatesGroup):
    """Владелец добавляет водителя: имя → телефон → тип ЗП → ставка."""
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_salary_type = State()
    waiting_for_salary_rate = State()


class AddVehicle(StatesGroup):
    """Владелец добавляет машину: номер → марка → тип → норма расхода."""
    waiting_for_plate = State()
    waiting_for_brand = State()
    waiting_for_type = State()
    waiting_for_fuel_norm = State()


# ========== ВОДИТЕЛЬ ==========
class StartShift(StatesGroup):
    """Начало смены: выбор машины → фото одометра → значение одометра."""
    selecting_vehicle = State()
    waiting_for_odometer_photo = State()
    waiting_for_odometer_value = State()


class EndShift(StatesGroup):
    """Завершение смены: фото одометра → значение одометра."""
    waiting_for_odometer_photo = State()
    waiting_for_odometer_value = State()


class NewTrip(StatesGroup):
    """Создание рейса: откуда → куда → груз."""
    waiting_for_origin = State()
    waiting_for_destination = State()
    waiting_for_cargo = State()


class UploadWaybill(StatesGroup):
    """Загрузка фото ТТН."""
    waiting_for_photo = State()


class EndTrip(StatesGroup):
    """Завершение рейса: выручка → расход топлива в литрах."""
    waiting_for_revenue = State()
    waiting_for_fuel_liters = State()


class NewExpense(StatesGroup):
    """Создание расхода: категория → сумма → фото чека (опционально)."""
    selecting_category = State()
    waiting_for_amount = State()
    waiting_for_receipt = State()
