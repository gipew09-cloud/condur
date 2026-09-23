"""
Самопроверка проекта: пройти по ВСЕМУ кабинету и по действиям водителя
и показать, где что-то падает.

Зачем. Владелец 22.09.2026: «протестируй весь проект, абсолютно весь, на
какие-то баги и ошибки». Тесты проверяют то, что мы заранее описали; эта
проверка — наоборот, тупо обходит всё подряд и смотрит, что сломается.

Запуск:
    .venv/bin/python -m app.tools.selfcheck

Поднимает кабинет на временной базе SQLite, наполняет показательными
данными (той же рукой, что витрина), затем:
  1. открывает КАЖДУЮ страницу и каждый адрес API, подставляя настоящие
     номера машин, водителей, смен, рейсов и расходов;
  2. прогоняет полный день водителя из приложения: код доступа → вход →
     фото → смена → рейс → выезд → сдал груз → расход → SOS → конец смены;
  3. проверяет, что чужого не отдаём: без входа и с чужим номером.

⚠️ Это НЕ замена тестам. Тесты ловят смысл, самопроверка — падения.
"""
import asyncio
import json
import os
import re
import sys
import traceback
import uuid
from pathlib import Path

ПАПКА = Path(sys.argv[1] if len(sys.argv) > 1 else
             "/tmp/condur-selfcheck")
ПАПКА.mkdir(parents=True, exist_ok=True)
БАЗА = ПАПКА / "проверка.sqlite3"
if БАЗА.exists():
    БАЗА.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{БАЗА}"
os.environ.setdefault("JWT_SECRET", "selfcheck")

from app.tools import showcase                                     # noqa: E402
from app.services import auth_service                              # noqa: E402

находки: list[str] = []
проверок = 0


def плохо(что: str) -> None:
    находки.append(что)
    print("  ✗", что)


def хорошо(что: str) -> None:
    global проверок
    проверок += 1
    print("  ·", что)


async def зайти(кабинет, адрес, куки, метод="GET", тело=None, тип=None):
    """Запрос в приложение напрямую. Возвращает (код, текст)."""
    статус, куски = [500], []

    отдано = {"было": False}

    async def принять():
        # ⚠️ Потоковые ответы (выгрузки .xlsx) после тела ждут не новый запрос,
        # а сообщение о разъединении. Иначе падает «Unexpected message».
        if отдано["было"]:
            return {"type": "http.disconnect"}
        отдано["было"] = True
        return {"type": "http.request", "body": тело or b"", "more_body": False}

    async def отдать(с):
        if с["type"] == "http.response.start":
            статус[0] = с["status"]
        elif с["type"] == "http.response.body":
            куски.append(с.get("body", b""))

    путь, _, запрос = адрес.partition("?")
    заголовки = [(b"host", b"selfcheck"), (b"cookie", куки.encode())]
    if тип:
        заголовки.append((b"content-type", тип.encode()))
    if тело:
        заголовки.append((b"content-length", str(len(тело)).encode()))
    await кабинет({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": метод, "scheme": "http",
        "path": путь, "raw_path": путь.encode(), "query_string": запрос.encode(),
        "root_path": "", "client": ("127.0.0.1", 50000), "server": ("selfcheck", 80),
        "headers": заголовки,
    }, принять, отдать)
    return статус[0], b"".join(куски).decode("utf-8", "replace")


async def главное() -> None:
    ключ = await showcase.наполнить()
    from app.web.router import app as кабинет
    from app.database import async_session
    from app.models import Driver, Expense, Shift, Trip, Vehicle
    from sqlalchemy import select

    class Молчун:
        def __getattr__(self, _):
            async def ничего(*a, **kw):
                return None
            return ничего

    кабинет.state.owner_bot = Молчун()
    кабинет.state.driver_bot = Молчун()
    куки = f"{auth_service.SESSION_COOKIE}={ключ}"

    async with async_session() as s:
        номера = {
            "vehicle_id": (await s.execute(select(Vehicle.id))).scalars().all(),
            "driver_id": (await s.execute(select(Driver.id))).scalars().all(),
            "shift_id": (await s.execute(select(Shift.id))).scalars().all(),
            "trip_id": (await s.execute(select(Trip.id))).scalars().all(),
            "expense_id": (await s.execute(select(Expense.id))).scalars().all(),
        }

    # ── 1. все страницы и адреса API ─────────────────────────────────────
    print("\n1. Страницы и API кабинета")
    адреса = []
    for маршрут in кабинет.routes:
        if "GET" not in getattr(маршрут, "methods", set()):
            continue
        путь = маршрут.path
        if путь.startswith(("/health", "/logout", "/.well-known")):
            continue
        поля = re.findall(r"\{(\w+)\}", путь)
        if not поля:
            адреса.append(путь)
            continue
        готов = путь
        пропустить = False
        for поле in поля:
            значения = номера.get(поле)
            if not значения:
                пропустить = True
                break
            готов = готов.replace("{" + поле + "}", str(значения[0]))
        if not пропустить:
            адреса.append(готов)

    # ⚠️ Этот адрес написан на «постгресовом» языке: DISTINCT ON и оператор
    # JSONB `payload ? 'lat'`. SQLite их не понимает, поэтому на временной базе
    # его не проверить — и, что важнее, его не покрыть НИ ОДНИМ тестом, потому
    # что все тесты проекта идут на SQLite. Проверять вживую.
    ТОЛЬКО_ПОСТГРЕС = {"/api/drivers-locations"}

    for адрес in sorted(set(адреса)):
        if адрес in ТОЛЬКО_ПОСТГРЕС:
            print(f"  ~ {адрес} — только на Postgres, здесь не проверяется")
            continue
        try:
            код, текст = await зайти(кабинет, адрес, куки)
        except Exception:                                    # noqa: BLE001
            плохо(f"{адрес} — падение: {traceback.format_exc(limit=1).strip().splitlines()[-1]}")
            continue
        if код >= 500:
            плохо(f"{адрес} — код {код}")
        elif код in (200, 303, 307, 401, 404, 422):
            хорошо(f"{адрес} → {код}")
        else:
            плохо(f"{адрес} — неожиданный код {код}")

    # ── 2. день водителя целиком ─────────────────────────────────────────
    print("\n2. День водителя из приложения")
    код, текст = await зайти(кабинет, "/drivers", куки)
    водитель = int(re.search(r"/drivers/(\d+)", текст).group(1))
    код, текст = await зайти(кабинет, f"/drivers/{водитель}/app/grant", куки, "POST", b"")
    совпало = re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", текст)
    if not совпало:
        плохо("владелец не смог выдать код доступа водителю")
        return
    хорошо("владелец выдал код доступа")

    телефон_куки = ""
    тело = json.dumps({"code": совпало.group(0), "device_id": "selfcheck-" + uuid.uuid4().hex[:8]}).encode()
    статус, куски = [500], []
    заголовки_ответа = []

    async def принять():
        return {"type": "http.request", "body": тело, "more_body": False}

    async def отдать(с):
        if с["type"] == "http.response.start":
            статус[0] = с["status"]
            заголовки_ответа.extend(с.get("headers", []))
        elif с["type"] == "http.response.body":
            куски.append(с.get("body", b""))

    await кабинет({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/driver/redeem", "raw_path": b"/api/driver/redeem",
        "query_string": b"", "root_path": "", "client": ("127.0.0.1", 50001),
        "server": ("selfcheck", 80),
        "headers": [(b"host", b"selfcheck"), (b"content-type", b"application/json"),
                    (b"content-length", str(len(тело)).encode())],
    }, принять, отдать)
    for имя, значение in заголовки_ответа:
        if имя.lower() == b"set-cookie":
            кусок = значение.decode().split(";")[0]
            if кусок.startswith("driver_session="):
                телефон_куки = кусок
    if статус[0] != 200 or not телефон_куки:
        плохо(f"приложение не вошло по коду (код {статус[0]})")
        return
    хорошо("приложение вошло по коду")

    async def действие(вид, полезное):
        тело = json.dumps({"client_op_id": uuid.uuid4().hex,
                           "type": вид, "payload": полезное}).encode()
        код, текст = await зайти(кабинет, "/api/driver/actions", телефон_куки,
                                 "POST", тело, "application/json")
        return код, (json.loads(текст) if текст else {})

    async with async_session() as s:
        машина = (await s.execute(select(Vehicle.id))).scalars().first()
        from app.models import RouteTemplate
        маршрут = (await s.execute(select(RouteTemplate.id))).scalars().first()

    # Фото одометра: без него смену не начать (FEATURE_ODOMETER_PHOTO).
    ПИКСЕЛЬ = __import__("base64").b64decode(
        "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
        "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAHwAA"
        "AQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQR"
        "BRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RF"
        "RkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ip"
        "qrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEB"
        "AAA/APn+iiigD//Z")

    async def загрузить_фото(вид):
        граница = "----selfcheck" + uuid.uuid4().hex[:8]
        части = []
        for имя, знач in (("client_id", "photo-" + uuid.uuid4().hex[:10]),
                          ("kind", вид), ("source", "camera")):
            части.append(f"--{граница}\r\nContent-Disposition: form-data; "
                         f"name=\"{имя}\"\r\n\r\n{знач}\r\n".encode())
        части.append(f"--{граница}\r\nContent-Disposition: form-data; name=\"file\"; "
                     f"filename=\"o.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode()
                     + ПИКСЕЛЬ + b"\r\n")
        части.append(f"--{граница}--\r\n".encode())
        код, текст = await зайти(кабинет, "/api/driver/photos", телефон_куки, "POST",
                                 b"".join(части),
                                 f"multipart/form-data; boundary={граница}")
        о = json.loads(текст) if текст else {}
        return код, о.get("photo")

    код, фото = await загрузить_фото("odometer_start")
    if код == 200 and фото:
        хорошо("фото одометра загрузилось")
    else:
        плохо(f"фото одометра не загрузилось (код {код})")
        фото = None

    шаги = [
        ("shift.start", {"vehicle_id": машина, "odometer": 100500, "photo": фото}),
        ("trip.create", {"template_id": маршрут}),
        ("trip.depart", {}),
        ("trip.finish", {"fuel": 120}),
        ("expense.create", {"category": "parking", "amount": "350"}),
        ("sos.send", {}),
        ("shift.finish", {"odometer": 100700, "photo": фото}),
    ]
    for вид, полезное in шаги:
        код, ответ = await действие(вид, полезное)
        if код >= 500:
            плохо(f"{вид} — код {код}")
        elif код != 200:
            плохо(f"{вид} — код {код}: {ответ.get('message')}")
        elif ответ.get("status") == "rejected":
            # Отказ — это не ошибка, если он объяснён по-человечески.
            причина = ((ответ.get("result") or {}).get("message")
                       or ответ.get("message") or "")
            if причина:
                хорошо(f"{вид} — отказ с объяснением: «{причина[:60]}»")
            else:
                плохо(f"{вид} — отказ БЕЗ объяснения")
        else:
            хорошо(f"{вид} — принято")

    # ── 3. чужое не отдаём ───────────────────────────────────────────────
    print("\n3. Чужое не отдаём")
    код, _ = await зайти(кабинет, "/expenses", "")
    if код in (303, 307, 401):
        хорошо(f"без входа кабинет отправляет на вход ({код})")
    else:
        плохо(f"без входа кабинет отдал код {код}")
    код, _ = await зайти(кабинет, "/api/driver/me", "")
    if код == 401:
        хорошо("без входа приложение получает отказ")
    else:
        плохо(f"без входа приложение получило код {код}")
    код, _ = await зайти(кабинет, "/vehicles/999999/edit", куки)
    if код in (404, 303):
        хорошо("чужая машина по номеру не открывается")
    else:
        плохо(f"чужая машина отдала код {код}")

    # ── итог ─────────────────────────────────────────────────────────────
    print()
    if находки:
        print(f"НАЙДЕНО ПРОБЛЕМ: {len(находки)} (проверок пройдено: {проверок})")
        for что in находки:
            print("  ✗", что)
    else:
        print(f"Проблем не найдено. Проверок пройдено: {проверок}")


async def main() -> None:
    from app.database import engine
    try:
        await главное()
    finally:
        await engine.dispose()
        if БАЗА.exists():
            БАЗА.unlink()
    sys.exit(1 if находки else 0)


if __name__ == "__main__":
    asyncio.run(main())
