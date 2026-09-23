"""
Витрина кабинета: настоящие страницы в виде отдельных HTML-файлов.

Зачем. Владелец 21.09.2026: «я не могу показать дизайнеру это в реальном
времени, могу только скидывать скрины, и он по ним обрабатывает — это плохо,
мне нужно очень хорошее решение».

Решение. Поднимаем кабинет на временной базе SQLite, наполняем её
показательными данными, обходим все страницы и сохраняем их HTML в папку.
Получается работающая копия кабинета: её можно открыть в браузере, кликать,
мерить линейкой разработчика, править стили прямо на месте. Не снимок.

Запуск:
    .venv/bin/python -m app.tools.showcase [папка]

По умолчанию папка — ~/Downloads/condur-витрина.

⚠️ Что здесь НЕ настоящее: карта Яндекса не поднимется без ключа (на её месте
честная подсказка), а данные вымышленные. Всё остальное — тот же шаблон, те
же стили и та же вёрстка, что видит владелец.
"""
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ПАПКА = Path(sys.argv[1] if len(sys.argv) > 1 else
             Path.home() / "Downloads" / "condur-витрина")
БАЗА = ПАПКА / "_витрина.sqlite3"

# ⚠️ Переменные задаём ДО импорта app.*: движок базы создаётся при импорте.
ПАПКА.mkdir(parents=True, exist_ok=True)
if БАЗА.exists():
    БАЗА.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{БАЗА}"
os.environ.setdefault("JWT_SECRET", "showcase")

from sqlalchemy import BigInteger                                  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB                   # noqa: E402
from sqlalchemy.ext.compiler import compiles                       # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):                          # noqa: D103
    return "JSON"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):                         # noqa: D103
    return "INTEGER"


from app.database import async_session, engine                     # noqa: E402
from app.models import (Base, Driver, Event, Expense, ExpenseCategory,  # noqa: E402
                        ManualEntry, Owner, RouteTemplate, Shift, Trip,
                        Vehicle, VehicleState, VehicleTelemetryPoint, WebSession)
from app.services import auth_service                              # noqa: E402

# ⚠️ Кабинет считает даты в поясе владельца функцией Postgres `timezone(...)`.
# В SQLite её нет — и страницы с итогами падали. Объясняем SQLite, что это
# такое: сдвинуть время из UTC в нужный пояс. Витрине этого достаточно.
from sqlalchemy import event                                       # noqa: E402
from zoneinfo import ZoneInfo                                      # noqa: E402


# ⚠️ SQLite возвращает время БЕЗ пояса, а кабинет сравнивает его с «сейчас»
# в UTC — и страницы с итогами падали на «can't compare offset-naive and
# offset-aware». Объявляем прочитанное время UTC: в базе витрины оно и есть UTC.
from sqlalchemy.dialects.sqlite import base as _sqlite_base        # noqa: E402

_исходный_разбор = _sqlite_base.DATETIME.result_processor


def _разбор_со_поясом(self, dialect, coltype):                     # noqa: D103
    дальше = _исходный_разбор(self, dialect, coltype)

    def обработать(значение):
        готово = дальше(значение) if дальше else значение
        if isinstance(готово, datetime) and готово.tzinfo is None:
            return готово.replace(tzinfo=timezone.utc)
        return готово

    return обработать


_sqlite_base.DATETIME.result_processor = _разбор_со_поясом


@event.listens_for(engine.sync_engine, "connect")
def _научить_sqlite(соединение, _):                                # noqa: D103
    def timezone(пояс, момент):
        if момент is None:
            return None
        try:
            когда = datetime.fromisoformat(str(момент))
        except ValueError:
            return момент
        if когда.tzinfo is None:
            когда = когда.replace(tzinfo=ZoneInfo("UTC"))
        return когда.astimezone(ZoneInfo(str(пояс))).strftime("%Y-%m-%d %H:%M:%S")

    соединение.create_function("timezone", 2, timezone)


# Страницы, которые обходим. Ключ — имя файла, значение — адрес в кабинете.
СТРАНИЦЫ = {
    "главная": "/",
    "мониторинг": "/map",
    "рейсы": "/trips",
    "смены": "/shifts",
    "финансы": "/finances",
    "расходы": "/finances/expenses",
    "доходы": "/finances/income",
    "акты": "/acts",
    "документы": "/documents",
    "водители": "/drivers",
    "машины": "/vehicles",
    "направления": "/routes",
    "статистика": "/stats",
    "реквизиты": "/requisites",
}


async def наполнить() -> str:
    """Демонстрационные данные. Возвращает ключ сессии для входа."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    сейчас = datetime.now(timezone.utc)
    async with async_session() as s:
        владелец = Owner(
            telegram_id=1, full_name="Иван Кибиткин", company_name="ИП Кибиткина",
            timezone="Europe/Moscow", notifications_enabled=True,
            executor_name="ИП Кибиткина Ирина Сергеевна", inn="780000000000",
        )
        s.add(владелец)
        await s.flush()

        машины = [
            Vehicle(owner_id=владелец.id, license_plate="Т772НХ178", color="white",
                    type="reefer", tank_litres=600, fuel_norm_per_100km=Decimal("32"),
                    stavtrack_object_id="128507", is_active=True),
            Vehicle(owner_id=владелец.id, license_plate="У774ЕТ178", color="yellow",
                    type="truck", tank_litres=400, is_active=True),
            Vehicle(owner_id=владелец.id, license_plate="Т557ОС178", color="blue",
                    type="reefer", tank_litres=600, is_active=True),
        ]
        s.add_all(машины)
        await s.flush()

        водители = [
            Driver(owner_id=владелец.id, telegram_id=1001, full_name="Саломов Д.",
                   default_vehicle_id=машины[0].id, salary_type="per_km",
                   salary_rate=Decimal("10"), is_active=True),
            Driver(owner_id=владелец.id, telegram_id=1002, full_name="Ахмедов Р.",
                   default_vehicle_id=машины[1].id, salary_type="per_trip",
                   salary_rate=Decimal("1500"), is_active=True),
        ]
        s.add_all(водители)
        await s.flush()

        s.add_all([
            RouteTemplate(owner_id=владелец.id, name="Кудрово", origin="Агропарк",
                          destination="Лента Кудрово", sort_order=10, is_active=True),
            RouteTemplate(owner_id=владелец.id, name="Шушары", origin="Агропарк",
                          destination="Магнит Шушары 177", default_cargo="Овощи",
                          sort_order=20, is_active=True),
        ])

        # Смена и рейсы за вчера — чтобы страницы были не пустыми.
        смена = Shift(
            owner_id=владелец.id, driver_id=водители[0].id, vehicle_id=машины[0].id,
            status="completed", started_at=сейчас - timedelta(hours=26),
            ended_at=сейчас - timedelta(hours=14),
            odometer_start=1693190, odometer_end=1693222,
        )
        s.add(смена)
        await s.flush()

        рейс = Trip(
            owner_id=владелец.id, driver_id=водители[0].id, vehicle_id=машины[0].id,
            shift_id=смена.id, origin="Агропарк", destination="Магнит Шушары 177",
            cargo_name="Овощи", status="completed",
            created_at=сейчас - timedelta(hours=25),
            completed_at=сейчас - timedelta(hours=16),
            revenue_rub=Decimal("18000"),
        )
        s.add(рейс)
        await s.flush()

        s.add_all([
            Expense(owner_id=владелец.id, driver_id=водители[0].id, shift_id=смена.id,
                    trip_id=рейс.id, category="fuel", amount_rub=Decimal("12400.50"),
                    status="approved", description="АЗС на Софийской",
                    created_at=сейчас - timedelta(hours=20)),
            Expense(owner_id=владелец.id, driver_id=водители[1].id, category="parking",
                    amount_rub=Decimal("700"), status="pending",
                    description="Стоянка на ночь", created_at=сейчас - timedelta(hours=3)),
        ])

        # Деньги за месяц — чтобы «Финансы» были с графиками, а не пустые:
        # рейсы с выручкой через день, траты водителей и владельца, одно
        # поступление по акту и один свой вид расхода.
        направления = [("Агропарк", "Магнит Шушары 177", 18000),
                       ("Агропарк", "Лента Кудрово", 16500),
                       ("Агропарк", "Пятёрочка Колпино", 14000)]
        for день in range(2, 22, 2):
            откуда, куда, выручка = направления[день % 3]
            s.add(Trip(
                owner_id=владелец.id, driver_id=водители[день % 2].id,
                vehicle_id=машины[день % 2].id, shift_id=смена.id,
                origin=откуда, destination=куда, cargo_name="Овощи", status="completed",
                created_at=сейчас - timedelta(days=день, hours=9),
                completed_at=сейчас - timedelta(days=день, hours=2),
                revenue_rub=Decimal(выручка),
            ))
            s.add(Expense(
                owner_id=владелец.id, driver_id=водители[день % 2].id, shift_id=смена.id,
                category="fuel", amount_rub=Decimal(9000 + день * 150), status="approved",
                description="Заправка", created_at=сейчас - timedelta(days=день, hours=5),
            ))
        s.add(ExpenseCategory(owner_id=владелец.id, name="Тахограф"))
        s.add_all([
            Expense(owner_id=владелец.id, vehicle_id=машины[0].id, category="repair",
                    amount_rub=Decimal("23500"), status="approved", created_by="owner",
                    payment_method="transfer", supplier="СТО «Грузовик-Сервис»",
                    description="Замена тормозных колодок",
                    spent_at=сейчас - timedelta(days=6, hours=3),
                    created_at=сейчас - timedelta(days=5)),
            Expense(owner_id=владелец.id, category="insurance", amount_rub=Decimal("41200"),
                    status="approved", created_by="owner", payment_method="transfer",
                    supplier="Ингосстрах", description="ОСАГО на год",
                    spent_at=сейчас - timedelta(days=12), created_at=сейчас - timedelta(days=12)),
            Expense(owner_id=владелец.id, vehicle_id=машины[1].id, category="Тахограф",
                    amount_rub=Decimal("4500"), status="approved", created_by="owner",
                    payment_method="card", description="Калибровка",
                    spent_at=сейчас - timedelta(days=9), created_at=сейчас - timedelta(days=9)),
            Expense(owner_id=владелец.id, driver_id=водители[1].id, category="toll",
                    amount_rub=Decimal("640"), status="approved", description="ЗСД",
                    created_at=сейчас - timedelta(days=3, hours=4)),
        ])
        s.add(ManualEntry(owner_id=владелец.id, type="income", category="Оплата по акту",
                          amount_rub=Decimal("60000"), description="Предоплата за октябрь",
                          entry_date=(сейчас - timedelta(days=4)).date()))

        # Немного телеметрии: точки и текущее состояние.
        широта, долгота = 59.7763, 30.4535
        for i in range(40):
            s.add(VehicleTelemetryPoint(
                owner_id=владелец.id, vehicle_id=машины[0].id,
                observed_at=сейчас - timedelta(minutes=(40 - i) * 3),
                latitude=Decimal(str(широта + i * 0.0012)),
                longitude=Decimal(str(долгота + i * 0.0021)),
                speed_kmh=Decimal("54") if i % 7 else Decimal("0"),
                course=Decimal("84"), ignition=True,
                voltage=Decimal("27.6") if i < 30 else Decimal("0"),
                mileage_km=Decimal(str(1693190 + i * 0.8)), is_valid=True,
                source="showcase",
            ))
        s.add_all([
            VehicleState(vehicle_id=машины[0].id,
                         latitude=Decimal(str(широта)), longitude=Decimal(str(долгота)),
                         speed_kmh=Decimal("0"), ignition=True, voltage=Decimal("27.9"),
                         motion_status="stopped", motion_since_at=сейчас - timedelta(minutes=32),
                         last_seen_at=сейчас - timedelta(seconds=49), is_valid=True),
            VehicleState(vehicle_id=машины[2].id,
                         latitude=Decimal("59.8573"), longitude=Decimal("30.4359"),
                         speed_kmh=Decimal("0"), ignition=False, voltage=Decimal("0"),
                         battery_voltage=Decimal("3.9"), motion_status="stopped",
                         motion_since_at=сейчас - timedelta(hours=9),
                         last_seen_at=сейчас - timedelta(minutes=32), is_valid=True),
        ])

        for вид, когда in (("shift_started", 26), ("trip_created", 25),
                           ("trip_completed", 16), ("expense_submitted", 20),
                           ("shift_completed", 14)):
            s.add(Event(owner_id=владелец.id, driver_id=водители[0].id,
                        shift_id=смена.id, event_type=вид, payload={},
                        created_at=сейчас - timedelta(hours=когда)))

        # Ключ сессии — чтобы обойти страницы как вошедший владелец.
        ключ = auth_service.new_session_token()
        s.add(WebSession(
            owner_id=владелец.id, telegram_id=владелец.telegram_id,
            token_hash=auth_service.session_token_hash(ключ),
            device_label="Витрина", last_seen_at=сейчас,
        ))
        await s.commit()
    return ключ


async def _запрос(кабинет, адрес: str, куки: str) -> tuple[int, str, str | None]:
    """Один GET прямо в приложение, без сетевой библиотеки.

    ⚠️ Нарочно без httpx: его нет в окружении проекта, а тащить ради витрины
    зависимость незачем. Кабинет — обычное ASGI-приложение, его можно позвать
    напрямую: собрать описание запроса и собрать ответ по кускам.
    """
    статус = [500]
    куски: list[bytes] = []
    куда: list[str] = []

    async def принять():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def отдать(сообщение):
        if сообщение["type"] == "http.response.start":
            статус[0] = сообщение["status"]
            for имя, значение in сообщение.get("headers", []):
                if имя.lower() == b"location":
                    куда.append(значение.decode())
        elif сообщение["type"] == "http.response.body":
            куски.append(сообщение.get("body", b""))

    путь, _, запрос = адрес.partition("?")
    описание = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": путь, "raw_path": путь.encode(), "query_string": запрос.encode(),
        "root_path": "", "client": ("127.0.0.1", 50000), "server": ("showcase", 80),
        "headers": [
            (b"host", b"showcase"),
            (b"cookie", куки.encode()),
            (b"user-agent", b"Condur showcase"),
            (b"accept", b"text/html"),
        ],
    }
    await кабинет(описание, принять, отдать)
    return статус[0], b"".join(куски).decode("utf-8", "replace"), (куда[0] if куда else None)


async def снять(ключ: str) -> list[tuple[str, int, int]]:
    """Обойти страницы и сохранить их HTML. Возвращает отчёт."""
    from app.web.router import app as кабинет

    # Бота нет — все уведомления просто не уходят.
    class Молчун:
        def __getattr__(self, _):
            async def ничего(*a, **kw):
                return None
            return ничего

    кабинет.state.owner_bot = Молчун()
    кабинет.state.driver_bot = Молчун()
    куки = f"{auth_service.SESSION_COOKIE}={ключ}"

    отчёт = []
    for имя, адрес in СТРАНИЦЫ.items():
        try:
            код, html, куда = await _запрос(кабинет, адрес, куки)
            # Переход на страницу входа означает, что сессия не подошла.
            for _ in range(3):
                if код not in (301, 302, 303, 307, 308) or not куда:
                    break
                код, html, куда = await _запрос(кабинет, куда, куки)
        except Exception as ошибка:                      # noqa: BLE001
            отчёт.append((имя, 0, 0))
            print(f"  {имя:14} ОШИБКА {type(ошибка).__name__}: {ошибка}")
            continue
        # Внутренние ссылки — на соседние файлы, чтобы витрина кликалась.
        # Ссылка с отбором («?period_from=…») ведёт на ту же страницу без
        # отбора: в витрине сервера нет, отбор всё равно не применится.
        for другое, куда_вело in СТРАНИЦЫ.items():
            html = re.sub(r'href="' + re.escape(куда_вело) + r'(?:\?[^"]*)?"',
                          f'href="{другое}.html"', html)
        # Свои картинки и стили лежат рядом, а не на сервере.
        html = html.replace('"/static/', '"static/').replace("'/static/", "'static/")
        (ПАПКА / f"{имя}.html").write_text(html, encoding="utf-8")
        отчёт.append((имя, код, len(html)))
        print(f"  {имя:14} {код}  {len(html) // 1024} КБ")
    return отчёт


def оглавление(отчёт) -> None:
    строки = "\n".join(
        f'    <li><a href="{имя}.html">{имя}</a> '
        f'<small>{код} · {размер // 1024} КБ</small></li>'
        for имя, код, размер in отчёт
    )
    (ПАПКА / "ОГЛАВЛЕНИЕ.html").write_text(f"""<!doctype html>
<meta charset="utf-8"><title>Condur — витрина кабинета</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #f1f5f9; margin: 0; padding: 40px; }}
  .box {{ max-width: 760px; margin: 0 auto; background: #fff; border-radius: 18px; padding: 28px 32px; }}
  h1 {{ margin: 0 0 6px; font-size: 22px; }}
  p {{ color: #475569; line-height: 1.5; }}
  ul {{ line-height: 2; padding-left: 20px; }}
  small {{ color: #94a3b8; }}
</style>
<div class="box">
  <h1>Condur — витрина кабинета</h1>
  <p>Настоящие страницы кабинета с показательными данными. Открывайте, кликайте,
     меряйте — это не снимки, а та же вёрстка и те же стили.</p>
  <p><b>Чего здесь нет:</b> карта Яндекса не поднимется без ключа — на её месте
     честная подсказка. Данные вымышленные.</p>
  <ul>
{строки}
  </ul>
</div>""", encoding="utf-8")


def перенести_статику() -> int:
    """Скопировать свои картинки и стили рядом со страницами."""
    import shutil

    откуда = Path(__file__).resolve().parents[1] / "web" / "static"
    куда = ПАПКА / "static"
    if куда.exists():
        shutil.rmtree(куда)
    if not откуда.exists():
        return 0
    shutil.copytree(откуда, куда)
    return sum(1 for _ in куда.rglob("*") if _.is_file())


async def main() -> None:
    print(f"Витрина: {ПАПКА}")
    ключ = await наполнить()
    отчёт = await снять(ключ)
    файлов = перенести_статику()
    print(f"  статика       {файлов} файлов")
    оглавление(отчёт)
    await engine.dispose()
    if БАЗА.exists():
        БАЗА.unlink()
    удачных = sum(1 for _, код, _ in отчёт if код == 200)
    print(f"\nГотово: {удачных} из {len(отчёт)} страниц. Открыть — ОГЛАВЛЕНИЕ.html")


if __name__ == "__main__":
    asyncio.run(main())
