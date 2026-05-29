"""
Все FSM-состояния ботов. StatesGroup = набор шагов в одном диалоге.

Важно: FSM — только подсказка для UI. Источник истины — БД.
В начале каждого хендлера сначала смотрим, что в БД, а не что в FSM.
"""
from aiogram.fsm.state import State, StatesGroup


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
