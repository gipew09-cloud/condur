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


class Onboarding(StatesGroup):
    """
    5-шаговый онбординг нового владельца. Заменяет короткую регистрацию,
    создаёт сразу машину, водителя и шаблон маршрута — чтобы человек
    через 10 минут мог открыть первую смену.
    """
    company = State()
    phone = State()
    vehicle_plate = State()
    vehicle_brand = State()
    driver_name = State()
    driver_phone = State()
    route_from = State()
    route_to = State()


class AddDriver(StatesGroup):
    """Владелец добавляет водителя: имя → телефон → ЗП → время старта смены."""
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_salary_type = State()
    waiting_for_salary_rate = State()
    waiting_for_shift_start = State()


class AddVehicle(StatesGroup):
    """Владелец добавляет машину: номер → марка → тип → расход → доки."""
    waiting_for_plate = State()
    waiting_for_brand = State()
    waiting_for_type = State()
    waiting_for_fuel_norm = State()
    waiting_for_osago = State()
    waiting_for_inspection = State()
    waiting_for_tacho = State()


class AddRouteTemplate(StatesGroup):
    """Владелец добавляет шаблон маршрута."""
    waiting_for_name = State()
    waiting_for_origin = State()
    waiting_for_destination = State()
    waiting_for_cargo = State()


class SetTripRevenue(StatesGroup):
    """Владелец вводит выручку завершённого рейса."""
    waiting_for_amount = State()


class TripCalc(StatesGroup):
    """Быстрый калькулятор рейса без записи в БД."""
    waiting_for_distance = State()
    waiting_for_rate = State()
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


class EndTripLocation(StatesGroup):
    """Перед завершением рейса спрашиваем геопозицию."""
    waiting_for_location = State()


class UnloadingLocation(StatesGroup):
    """Перед переходом в статус 'выгрузка' спрашиваем геопозицию."""
    waiting_for_location = State()


class NewExpense(StatesGroup):
    """Создание расхода: категория → сумма → фото чека (опционально)."""
    selecting_category = State()
    waiting_for_amount = State()
    waiting_for_receipt = State()


class HandedCash(StatesGroup):
    """Водитель сообщает что отдал нал владельцу."""
    waiting_for_amount = State()
    waiting_for_photo = State()
