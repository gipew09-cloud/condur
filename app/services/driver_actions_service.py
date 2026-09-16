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

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Driver, DriverAction, DriverSession, Shift, Vehicle
from app.services import shift_flow, shift_service

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


# ---------------------------------------------------------------- обработчики
async def _shift_start(session, driver: Driver, payload: dict, moment: datetime):
    vehicle_id = _int_or_none(payload.get("vehicle_id"), field_name="vehicle_id")
    if vehicle_id is None:
        raise BadRequest("vehicle_id обязателен")
    odometer = _odometer(payload.get("odometer"))

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

    opened = await shift_flow.open_shift(
        session, driver=driver, vehicle=vehicle,
        odometer_start=odometer, photo_ref=None, source="app", now=moment,
    )
    result = {
        "shift_id": opened.shift.id,
        "vehicle_id": vehicle.id,
        "plate": vehicle.license_plate,
        "started_at": opened.started_at.isoformat(),
    }
    return result, {"kind": "shift_started", "opened": opened, "vehicle": vehicle}


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
    if odometer is not None and active.odometer_start is not None and odometer < active.odometer_start:
        raise ActionRejected(
            "odometer_backwards",
            f"Одометр меньше, чем в начале смены ({active.odometer_start} км). Проверьте цифры.",
        )
    started = _utc(active.started_at)
    if started is not None and moment < started:
        # Часы телефона отстают сильнее, чем длилась смена: берём время сервера.
        moment = datetime.now(timezone.utc)

    closed = await shift_flow.close_shift(
        session, driver=driver, shift=active,
        odometer_end=odometer, photo_ref=None, source="app", now=moment,
    )
    # ⚠️ Зарплату водителю не показываем — решение владельца (как в боте).
    result = {
        "shift_id": closed.shift.id,
        "ended_at": closed.ended_at.isoformat(),
        "distance_km": closed.shift.distance_km,
        "trips": len(closed.trips),
    }
    return result, {"kind": "shift_completed", "closed": closed}


HANDLERS = {
    "shift.start": _shift_start,
    "shift.finish": _shift_finish,
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


def owner_notice(outcome: Outcome, *, driver: Driver, tz_name: str | None) -> str | None:
    """Текст владельцу — тот же, что шлёт бот (общий shift_flow)."""
    after = outcome.after_commit
    if outcome.duplicate or not after:
        return None
    if after.get("kind") == "shift_started":
        return shift_flow.shift_started_owner_text(
            after["opened"], driver=driver, vehicle=after["vehicle"], tz_name=tz_name
        )
    if after.get("kind") == "shift_completed":
        return shift_flow.shift_completed_owner_text(
            after["closed"], driver=driver, tz_name=tz_name
        )
    return None


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
