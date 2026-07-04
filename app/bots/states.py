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


class EditExpenseAmount(StatesGroup):
    """Владелец правит сумму расхода (если бот/водитель ошиблись), Блок C."""
    waiting_for_amount = State()


class SetOdometer(StatesGroup):
    """Владелец вписывает пробег по фото одометра от водителя (Правка 1).
    В data храним shift_id и which ('start'/'end')."""
    waiting_for_value = State()


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


class TripDepartLocation(StatesGroup):
    """Перед выездом («Выехал») спрашиваем геопозицию (Правка 2)."""
    waiting_for_location = State()


class EndTripLocation(StatesGroup):
    """Перед завершением рейса спрашиваем геопозицию."""
    waiting_for_location = State()


class WipeAll(StatesGroup):
    """Скрытая команда /wipe (нет в /help): полный сброс тестовых данных
    владельца. Ждём точную фразу подтверждения."""
    waiting_for_confirm = State()


class EndShiftLocation(StatesGroup):
    """После завершения смены спрашиваем, где водитель её закончил.

    Владелец получает точку ссылкой на Яндекс.Карты — контроль «где реально
    закончился день», особенно когда GPS-трекер в городе лагает."""
    waiting_for_location = State()


class UnloadingLocation(StatesGroup):
    """Перед переходом в статус 'выгрузка' спрашиваем геопозицию."""
    waiting_for_location = State()


class DriverTripRevenue(StatesGroup):
    """Водитель по желанию указывает выручку завершённого рейса (он отдал груз)."""
    waiting_for_amount = State()


class NewExpense(StatesGroup):
    """Создание расхода: категория → сумма → (для «Прочее» — описание) → фото чека."""
    selecting_category = State()
    waiting_for_amount = State()
    waiting_for_description = State()
    waiting_for_receipt = State()


class HandedCash(StatesGroup):
    """Водитель сообщает что отдал нал владельцу."""
    waiting_for_amount = State()
    waiting_for_photo = State()


class AddManualShift(StatesGroup):
    """Оффлайн-добавление смены задним числом (Блок D): машина → дата."""
    selecting_vehicle = State()
    waiting_for_date = State()


class AddManualTrip(StatesGroup):
    """Оффлайн-добавление рейса задним числом (Блок D): машина → маршрут → дата."""
    selecting_vehicle = State()
    waiting_for_origin = State()
    waiting_for_destination = State()
    waiting_for_date = State()
