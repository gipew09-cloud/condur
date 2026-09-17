"""
Действия водителя из приложения — с защитой от повторов.

Как это работает (сошлись семь нейросетей, `DRIVER_ACCESS_AI_ANSWERS.md`):
1. Телефон сначала сохраняет действие у себя и даёт ему номер (`client_op_id`).
2. Отправляет, когда появится связь. Не дошёл ответ — отправляет ТОТ ЖЕ номер.
3. Сервер хранит номер с уникальным индексом: пришёл повтор — возвращает
   прежний ответ, вторую смену не создаёт.

Отказ («смена уже открыта») — тоже окончательный ответ и тоже запоминается:
телефон перестаёт повторять и показывает водителю причину.

Коммит делает вызывающий: действие и запись о нём ложатся одной транзакцией.
Если два одинаковых запроса пришли одновременно, второй упрётся в уникальный
индекс, его транзакция откатится ЦЕЛИКОМ (вместе с лишней сменой), и он
вернёт то, что записал первый.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import keyboards as kb
from app.bots import messages as msg
from app.config import settings
from app.models import (
    Driver, DriverAction, DriverPhoto, DriverSession, RouteTemplate, Shift, Vehicle,
)
from app.services import (
    driver_photos, expense_flow, shift_flow, shift_service, telemetry_service, trip_flow,
    trip_service,
)
from app.services.event_service import log_event

# Часам телефона верим, только если они правдоподобны: действие, сохранённое
# без связи, могло пролежать на телефоне до суток; из будущего — не бывает.
CLIENT_TIME_MAX_AGE = timedelta(hours=24)
CLIENT_TIME_MAX_AHEAD = timedelta(minutes=2)

ODOMETER_MAX = 9_999_999


class ActionRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class BadRequest(Exception):
    """Запрос собран неправильно — это ошибка приложения, а не отказ по делу."""


@dataclass
class Outcome:
    action: DriverAction
    duplicate: bool
    # Что понадобится после коммита, чтобы известить владельца.
    after_commit: dict = field(default_factory=dict)

    def body(self) -> dict:
        a = self.action
        data = {
            "ok": a.status == "accepted",
            "status": a.status,
            "client_op_id": a.client_op_id,
            "type": a.action_type,
            "duplicate": self.duplicate,
            "received_at": _utc(a.received_at).isoformat() if a.received_at else None,
        }
        data.update(a.result or {})
        return data


def _utc(moment: datetime | None) -> datetime | None:
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def parse_client_time(raw, now: datetime) -> datetime | None:
    """Время нажатия с телефона, если ему можно верить. Иначе None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    moment = moment.astimezone(timezone.utc)
    if moment > now + CLIENT_TIME_MAX_AHEAD or moment < now - CLIENT_TIME_MAX_AGE:
        return None
    return moment


def _int_or_none(value, *, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BadRequest(f"{field_name}: ожидалось число")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{field_name}: ожидалось число")
    return number


def _odometer(value) -> int | None:
    km = _int_or_none(value, field_name="odometer")
    if km is not None and not (0 < km <= ODOMETER_MAX):
        raise ActionRejected("bad_odometer", "Показания одометра выглядят неверно. Проверьте цифры.")
    return km


async def _photo(session, driver: Driver, payload: dict) -> DriverPhoto | None:
    """Фото, на которое ссылается действие. Телефон сначала загружает фото,
    потом шлёт действие со ссылкой `app-<id>`; чужое фото не примем."""
    ref = payload.get("photo")
    if ref is None or ref == "":
        return None
    if not isinstance(ref, str) or not driver_photos.is_app_ref(ref):
        raise BadRequest("photo: ожидалась ссылка app-<id>")
    photo = await driver_photos.for_driver(session, driver.id, ref)
    if photo is None:
        raise ActionRejected("photo_missing", "Фото не дошло до сервера. Снимите ещё раз.")
    return photo


def _photo_required() -> bool:
    """Фото одометра обязательно — как в боте (FEATURE_ODOMETER_PHOTO)."""
    return bool(settings.feature_odometer_photo)


# ---------------------------------------------------------------- обработчики
async def _shift_start(session, driver: Driver, payload: dict, moment: datetime):
    vehicle_id = _int_or_none(payload.get("vehicle_id"), field_name="vehicle_id")
    if vehicle_id is None:
        raise BadRequest("vehicle_id обязателен")
    odometer = _odometer(payload.get("odometer"))

    # ⚠️ Замки на водителя и машину (аудит 17.09): два телефона разом не
    # откроют две смены — второй запрос ждёт первого и видит его смену.
    await session.get(Driver, driver.id, with_for_update=True)
    await session.get(Vehicle, vehicle_id, with_for_update=True)
    active = await shift_service.get_active_shift(session, driver.id)
    if active is not None:
        raise ActionRejected(
            "shift_already_open",
            "Смена уже открыта. Сначала закончите её.",
        )
    vehicle = await session.get(Vehicle, vehicle_id)
    if vehicle is None or vehicle.owner_id != driver.owner_id or not vehicle.is_active:
        raise ActionRejected("vehicle_unknown", "Такой машины нет в парке.")
    busy = (await session.execute(
        select(Shift.id).where(
            Shift.vehicle_id == vehicle.id, Shift.status == "started",
            Shift.driver_id != driver.id,
        ).limit(1)
    )).scalar_one_or_none()
    if busy is not None:
        raise ActionRejected(
            "vehicle_busy",
            f"На {vehicle.license_plate} уже открыта смена другого водителя.",
        )

    photo = await _photo(session, driver, payload)
    if photo is None and _photo_required():
        raise ActionRejected(
            "photo_required", "Сфотографируйте одометр — без фото смену не начать."
        )
    opened = await shift_flow.open_shift(
        session, driver=driver, vehicle=vehicle,
        odometer_start=odometer, photo_ref=driver_photos.ref_of(photo) if photo else None,
        source="app", now=moment,
    )
    result = {
        "shift_id": opened.shift.id,
        "vehicle_id": vehicle.id,
        "plate": vehicle.license_plate,
        "started_at": opened.started_at.isoformat(),
    }
    return result, {
        "kind": "shift_started", "opened": opened, "vehicle": vehicle, "photo": photo,
    }


async def _shift_finish(session, driver: Driver, payload: dict, moment: datetime):
    odometer = _odometer(payload.get("odometer"))
    wanted = _int_or_none(payload.get("shift_id"), field_name="shift_id")

    active = await shift_service.get_active_shift(session, driver.id)
    if active is None:
        raise ActionRejected("no_open_shift", "Открытой смены нет — закрывать нечего.")
    if wanted is not None and wanted != active.id:
        raise ActionRejected(
            "shift_mismatch",
            "Эта смена уже закрыта. Откройте приложение заново — покажем текущую.",
        )
    # Как в боте: смену с открытым рейсом не закрываем. 16.09 приложение
    # это пропускало — нашлось, когда рейсы переезжали в приложение.
    if await trip_service.get_active_trip(session, active.id) is not None:
        raise ActionRejected("trip_open", msg.SHIFT_TRIP_OPEN_CANT_END)
    if odometer is not None and active.odometer_start is not None and odometer < active.odometer_start:
        raise ActionRejected(
            "odometer_backwards",
            f"Одометр меньше, чем в начале смены ({active.odometer_start} км). Проверьте цифры.",
        )
    photo = await _photo(session, driver, payload)
    if photo is None and _photo_required():
        raise ActionRejected(
            "photo_required", "Сфотографируйте одометр — без фото смену не закончить."
        )
    started = _utc(active.started_at)
    if started is not None and moment < started:
        # Часы телефона отстают сильнее, чем длилась смена: берём время сервера.
        moment = datetime.now(timezone.utc)

    # То же самое фото, что утром (случай 17.09, смена 119)?
    same_photo = False
    if photo is not None:
        first = await driver_photos.for_driver(
            session, driver.id, active.odometer_start_photo_url,
        )
        same_photo = first is not None and first.sha256 == photo.sha256
    vehicle = await session.get(Vehicle, active.vehicle_id)

    closed = await shift_flow.close_shift(
        session, driver=driver, shift=active,
        odometer_end=odometer,
        photo_ref=driver_photos.ref_of(photo) if photo else None,
        source="app", now=moment,
    )
    # ⚠️ Зарплату водителю не показываем — решение владельца (как в боте).
    result = {
        "shift_id": closed.shift.id,
        "ended_at": closed.ended_at.isoformat(),
        "distance_km": closed.shift.distance_km,
        "trips": len(closed.trips),
    }
    return result, {
        "kind": "shift_completed", "closed": closed, "photo": photo,
        "same_photo": same_photo, "vehicle": vehicle,
    }


async def _open_shift(session, driver: Driver) -> Shift:
    shift = await shift_service.get_active_shift(session, driver.id)
    if shift is None:
        raise ActionRejected("no_open_shift", msg.TRIP_NEED_SHIFT)
    return shift


async def _open_trip(session, driver: Driver, payload: dict):
    """Открытый рейс, о котором говорит телефон. Номер рейса телефон может и
    не знать (рейс создан без связи) — тогда берём открытый."""
    shift = await _open_shift(session, driver)
    wanted = _int_or_none(payload.get("trip_id"), field_name="trip_id")
    trip = await trip_service.get_active_trip(session, shift.id)
    if trip is None:
        raise ActionRejected("no_open_trip", msg.TRIP_NO_ACTIVE)
    if wanted is not None and wanted != trip.id:
        raise ActionRejected(
            "trip_mismatch",
            "Этот рейс уже закрыт. Потяните экран вниз — покажем текущий.",
        )
    return shift, trip


def _trip_result(trip) -> dict:
    return {"trip_id": trip.id, "trip_status": trip.status}


async def _trip_create(session, driver: Driver, payload: dict, moment: datetime):
    template_id = _int_or_none(payload.get("template_id"), field_name="template_id")
    if template_id is None:
        raise BadRequest("template_id обязателен")
    shift = await _open_shift(session, driver)
    # Замок на смену: два «Новый рейс» разом не создадут два рейса.
    await session.get(Shift, shift.id, with_for_update=True)
    if await trip_service.get_active_trip(session, shift.id) is not None:
        raise ActionRejected("trip_already_open", msg.TRIP_ALREADY_OPEN)
    template = await session.get(RouteTemplate, template_id)
    if template is None or template.owner_id != driver.owner_id or not template.is_active:
        raise ActionRejected(
            "route_unknown",
            "Этого маршрута больше нет. Потяните экран вниз — список обновится.",
        )
    trip = await trip_flow.create_trip(
        session, driver=driver, shift=shift,
        origin=template.origin, destination=template.destination,
        cargo=template.default_cargo, source="app", now=moment,
    )
    return _trip_result(trip), {"kind": "trip_created", "trip": trip}


async def _trip_depart(session, driver: Driver, payload: dict, moment: datetime):
    shift, trip = await _open_trip(session, driver, payload)
    if trip.status != "created":
        raise ActionRejected("trip_wrong_status", "Выезд уже отмечен.")
    await trip_flow.depart(session, driver=driver, shift=shift, trip=trip, source="app")
    return _trip_result(trip), {"kind": "trip_departed", "trip": trip}


async def _trip_unloading(session, driver: Driver, payload: dict, moment: datetime):
    shift, trip = await _open_trip(session, driver, payload)
    if not settings.feature_trip_status_steps:
        raise ActionRejected("step_disabled", msg.TRIP_WRONG_STATUS)
    if trip.status != "in_transit":
        raise ActionRejected("trip_wrong_status", msg.TRIP_WRONG_STATUS)
    await trip_flow.start_unloading(session, driver=driver, shift=shift, trip=trip, source="app")
    return _trip_result(trip), {"kind": "trip_unloading", "trip": trip}


async def _trip_finish(session, driver: Driver, payload: dict, moment: datetime):
    shift, trip = await _open_trip(session, driver, payload)
    if not trip_flow.can_finish(trip.status):
        raise ActionRejected(
            "trip_wrong_status",
            "Сначала отметьте «Выехал»." if trip.status == "created" else msg.TRIP_WRONG_STATUS,
        )
    created = _utc(trip.created_at)
    fuel = await trip_flow.finish(
        session, driver=driver, shift=shift, trip=trip, source="app",
        # Часы телефона раньше начала рейса — берём время сервера.
        now=moment if created is None or moment >= created else None,
    )
    # ⚠️ Деньги по рейсу водителю не отдаём: в топливо могут входить расходы,
    # которые владелец отклонил, а водитель этого видеть не должен.
    return _trip_result(trip), {"kind": "trip_completed", "trip": trip, "fuel": fuel}


async def _trip_waybill(session, driver: Driver, payload: dict, moment: datetime):
    """Фото ТТН к открытому рейсу — как «📄 Загрузить ТТН» в боте."""
    shift, trip = await _open_trip(session, driver, payload)
    photo = await _photo(session, driver, payload)
    if photo is None:
        raise BadRequest("photo обязателен")
    await trip_service.attach_waybill(
        session, trip=trip, photo_file_id=driver_photos.ref_of(photo),
    )
    await log_event(
        session, owner_id=driver.owner_id, driver_id=driver.id,
        shift_id=shift.id, trip_id=trip.id, event_type="waybill_uploaded",
        payload={"source": "app"},
    )
    return _trip_result(trip), {"kind": "trip_waybill", "trip": trip, "photo": photo}


async def _expense_create(session, driver: Driver, payload: dict, moment: datetime):
    """Расход — как «💳 Расход» в боте (общая логика `expense_flow`).
    Смена не нужна; чек по желанию. Решение владельца телефону не уходит."""
    category = payload.get("category")
    if not isinstance(category, str) or not category:
        raise BadRequest("category обязателен")
    amount = expense_flow.parse_amount(payload.get("amount"))
    if amount is None or amount <= 0:
        raise ActionRejected("bad_amount", "Не похоже на сумму. Введите число.")
    if amount > expense_flow.MAX_AMOUNT:
        raise ActionRejected("bad_amount", "Слишком большая сумма. Проверьте цифры.")
    amount = amount.quantize(Decimal("0.01"))
    description = expense_flow.clean_description(payload.get("description"))
    if category == "other" and not description:
        raise ActionRejected("description_required", "Коротко напишите, что за расход.")
    photo = await _photo(session, driver, payload)
    try:
        await expense_flow.ensure_can_submit(session, driver)
        submitted = await expense_flow.submit(
            session, driver=driver, category=category, amount=amount,
            description=description,
            receipt_ref=driver_photos.ref_of(photo) if photo is not None else None,
            source="app",
        )
    except expense_flow.Refused as refused:
        raise ActionRejected("expense_refused", str(refused)) from None
    return {"expense_id": submitted.expense.id}, {
        "kind": "expense_submitted", "submitted": submitted,
        "photo": photo, "description": description,
    }


async def _sos_send(session, driver: Driver, payload: dict, moment: datetime):
    """SOS — как «🆘 SOS» в боте: событие и сообщение владельцу."""
    sent = await expense_flow.send_sos(session, driver=driver, source="app")
    return {}, {"kind": "sos", "sos": sent}


HANDLERS = {
    "shift.start": _shift_start,
    "shift.finish": _shift_finish,
    "trip.create": _trip_create,
    "trip.depart": _trip_depart,
    "trip.unloading": _trip_unloading,
    "trip.finish": _trip_finish,
    "trip.waybill": _trip_waybill,
    "expense.create": _expense_create,
    "sos.send": _sos_send,
}


# ------------------------------------------------------------------- главное
async def find_existing(session: AsyncSession, driver_id: int, op_id: str) -> DriverAction | None:
    return (await session.execute(
        select(DriverAction).where(
            DriverAction.driver_id == driver_id,
            DriverAction.client_op_id == op_id,
        )
    )).scalar_one_or_none()


def _validate_envelope(body: dict) -> tuple[str, str, dict, int | None]:
    if not isinstance(body, dict):
        raise BadRequest("ожидался объект")
    op_id = body.get("client_op_id")
    if not isinstance(op_id, str) or not (8 <= len(op_id.strip()) <= 64):
        raise BadRequest("client_op_id: строка от 8 до 64 символов")
    kind = body.get("type")
    if not isinstance(kind, str) or not kind:
        raise BadRequest("type обязателен")
    payload = body.get("payload") or {}
    if not isinstance(payload, dict):
        raise BadRequest("payload: ожидался объект")
    seq = _int_or_none(body.get("device_seq"), field_name="device_seq")
    return op_id.strip(), kind, payload, seq


async def apply(
    session: AsyncSession,
    *,
    driver_session: DriverSession,
    driver: Driver,
    body: dict,
    now: datetime | None = None,
) -> Outcome:
    """Принять действие. Повтор с тем же номером возвращает прежний ответ."""
    now = now or datetime.now(timezone.utc)
    op_id, kind, payload, seq = _validate_envelope(body)

    existing = await find_existing(session, driver.id, op_id)
    if existing is not None:
        return Outcome(action=existing, duplicate=True)

    handler = HANDLERS.get(kind)
    if handler is None:
        raise BadRequest(f"неизвестное действие: {kind}")

    client_time = parse_client_time(body.get("client_created_at"), now)
    moment = client_time or now
    action = DriverAction(
        owner_id=driver.owner_id,
        driver_id=driver.id,
        session_id=driver_session.id,
        device_id=driver_session.device_id,
        client_op_id=op_id,
        action_type=kind,
        payload=payload,
        device_seq=seq,
        client_created_at=client_time,
        received_at=now,
    )
    after: dict = {}
    try:
        # SAVEPOINT: при отказе откатываем только частично сделанное действием,
        # а запись об отказе сохраняем.
        async with session.begin_nested():
            result, after = await handler(session, driver, payload, moment)
        action.status = "accepted"
        action.result = result
    except ActionRejected as rejected:
        action.status = "rejected"
        action.result = {"error": rejected.code, "message": rejected.message}
        after = {}
    # ⚠️ Без flush: запись о действии уходит в базу при коммите, и дубль номера
    # всплывает ровно там, где его ловит commit_or_replay.
    session.add(action)
    return Outcome(action=action, duplicate=False, after_commit=after)


@dataclass
class OwnerNotice:
    """Что уйдёт владельцу. Есть фото — уходит фотографией с подписью `text`;
    `lead` — отдельное сообщение перед фото (итог смены, как в боте)."""
    text: str
    markup: object | None = None
    photo: bytes | None = None
    lead: str | None = None


def owner_message(outcome: Outcome, *, driver: Driver, tz_name: str | None) -> OwnerNotice | None:
    """Что уйдёт владельцу. Тексты те же, что шлёт бот — общие shift_flow и
    trip_flow. Повтор действия владельца не зовёт.

    К фото из приложения — строка, откуда оно: камера или галерея (владелец
    17.09.2026). У бота такой строки нет: Telegram этого не сообщает."""
    notice = _owner_message(outcome, driver=driver, tz_name=tz_name)
    photo = outcome.after_commit.get("photo") if notice is not None else None
    line = driver_photos.origin_line(getattr(photo, "source", None))
    if notice is not None and notice.photo is not None and line:
        notice.text = f"{notice.text}\n{line}"
    return notice


def _owner_message(outcome: Outcome, *, driver: Driver, tz_name: str | None) -> OwnerNotice | None:
    after = outcome.after_commit
    if outcome.duplicate or not after:
        return None
    kind = after.get("kind")
    photo = after.get("photo")
    photo_bytes = photo.data if photo is not None else None
    if kind == "shift_started":
        opened, vehicle = after["opened"], after["vehicle"]
        if photo is not None and opened.shift.odometer_start is None:
            # Как в боте: фото одометра с кнопкой «Указать пробег».
            caption = msg.ODOMETER_PHOTO_TO_OWNER_START.format(
                driver=driver.full_name, plate=vehicle.license_plate,
            )
            line = telemetry_service.ignition_shift_line(
                opened.ignition, moment=opened.started_at, tz_name=tz_name, closing=False,
            )
            if line:
                caption += f"\n{line}"
            return OwnerNotice(
                caption, kb.odometer_set_keyboard(opened.shift.id, "start"), photo_bytes,
            )
        text = shift_flow.shift_started_owner_text(
            opened, driver=driver, vehicle=vehicle, tz_name=tz_name,
        )
        return OwnerNotice(text, None, photo_bytes)
    if kind == "shift_completed":
        closed = after["closed"]
        text = shift_flow.shift_completed_owner_text(closed, driver=driver, tz_name=tz_name)
        if photo is not None and closed.shift.odometer_end is None:
            # Как в боте: сначала итог смены, потом фото одометра с кнопкой.
            vehicle = after.get("vehicle")
            caption = msg.ODOMETER_PHOTO_TO_OWNER_END.format(
                driver=driver.full_name,
                plate=vehicle.license_plate if vehicle is not None else "—",
            )
            return OwnerNotice(
                caption, kb.odometer_set_keyboard(closed.shift.id, "end"),
                photo_bytes, lead=text,
            )
        return OwnerNotice(text, None, photo_bytes)
    if kind == "expense_submitted":
        # Как в боте: чек фотографией (если есть) и кнопки решения.
        submitted = after["submitted"]
        return OwnerNotice(
            expense_flow.owner_caption(
                driver.full_name, submitted.category, submitted.amount,
                after.get("description"),
            ),
            expense_flow.owner_markup(submitted.expense.id),
            photo_bytes,
        )
    if kind == "sos":
        return OwnerNotice(after["sos"].owner_text(driver))
    trip = after.get("trip")
    if kind == "trip_created":
        return OwnerNotice(trip_flow.created_owner_text(trip, driver=driver))
    if kind == "trip_departed":
        return OwnerNotice(trip_flow.departed_owner_text(trip, driver=driver))
    if kind == "trip_unloading":
        return OwnerNotice(trip_flow.unloading_owner_text(trip, driver=driver))
    if kind == "trip_completed":
        return OwnerNotice(
            trip_flow.completed_owner_text(trip, driver=driver, fuel=after["fuel"]),
            trip_flow.revenue_markup(trip),
        )
    if kind == "trip_waybill":
        return OwnerNotice(
            msg.NOTIFY_WAYBILL.format(
                driver=driver.full_name,
                origin=trip.origin or "—", destination=trip.destination or "—",
            ),
            None,
            photo_bytes,
        )
    return None


def owner_notice(outcome: Outcome, *, driver: Driver, tz_name: str | None) -> str | None:
    """Только текст уведомления (см. owner_message)."""
    notice = owner_message(outcome, driver=driver, tz_name=tz_name)
    return notice.text if notice is not None else None


async def commit_or_replay(
    session: AsyncSession, *, driver_id: int, outcome: Outcome
) -> Outcome:
    """Сохранить действие. Если такой же номер успел записать соседний запрос —
    откатить своё целиком и вернуть его ответ.

    ⚠️ Откат здесь важен: вместе с записью о действии уходит и лишняя смена,
    которую успел создать второй запрос.
    """
    if outcome.duplicate:
        return outcome
    op_id = outcome.action.client_op_id
    try:
        await session.commit()
        return outcome
    except IntegrityError:
        await session.rollback()
        existing = await find_existing(session, driver_id, op_id)
        if existing is None:
            raise
        return Outcome(action=existing, duplicate=True)
