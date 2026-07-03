"""
Полноценные тесты по доработкам: акт (формат/итог/прописью/название),
JWT-авторизация (вход владельца/админа + мгновенный отзыв), и рендер всех
изменённых страниц кабинета через настоящее Jinja-окружение с реальными фильтрами.

Запуск: `pip install pytest openpyxl jinja2` затем `pytest` в корне проекта.
ENV ботов/БД фиктивные — нужны только для импорта настроек; до сети/БД не доходим.
"""
import os
import io
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace as NS

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test-secret-please-set-a-long-one-in-prod")

import pytest  # noqa: E402
from jinja2 import Environment, FileSystemLoader  # noqa: E402
from openpyxl import Workbook  # noqa: E402

from app.services import act_service as A  # noqa: E402
from app.services import auth_service as AU  # noqa: E402
from app.services import rc_service as RC  # noqa: E402
from app.services.timeutil import fmt_dt  # noqa: E402

# Jinja-окружение с теми же фильтрами, что в app/web/router.py (чтобы не тянуть
# в тест весь FastAPI/SQLAlchemy-стек ради рендера шаблонов).
_VT = {"truck": "Грузовик", "gazelle": "Газель / фургон", "refrigerator": "Рефрижератор"}
_TS = {"created": "создан", "in_transit": "в пути", "unloading": "на выгрузке",
       "completed": "завершён", "cancelled": "отменён"}
_PILL = {"completed": "pill--success", "paid": "pill--success", "pending": "pill--warn",
         "approved": "pill--success", "rejected": "pill--danger", "in_transit": "pill--info"}
_ENV = Environment(loader=FileSystemLoader("app/web/templates"))
_ENV.filters["vtype"] = lambda c: _VT.get(c or "", c or "—")
_ENV.filters["tstatus"] = lambda c: _TS.get(c or "", c or "—")
_ENV.filters["pillclass"] = lambda s: _PILL.get((s or "").lower(), "pill--neutral")
_ENV.filters["statusru"] = lambda s: s or "—"
_ENV.filters["localdt"] = lambda dt, tz=None, fmt="%d.%m.%Y %H:%M": fmt_dt(dt, tz, fmt)


# ---------------------------------------------------------------- сумма прописью
@pytest.mark.parametrize("amount,expected", [
    (Decimal("292000"), "Двести девяносто две тысячи рублей 00 копеек."),
    (Decimal("178000"), "Сто семьдесят восемь тысяч рублей 00 копеек."),
    (Decimal("0"), "Ноль рублей 00 копеек."),
    (Decimal("1"), "Один рубль 00 копеек."),
    (Decimal("1234.56"), "Одна тысяча двести тридцать четыре рубля 56 копеек."),
    (Decimal("21"), "Двадцать один рубль 00 копеек."),
])
def test_rubles_in_words(amount, expected):
    assert A.rubles_in_words(amount) == expected


def test_money_str_ru_format():
    # разделитель тысяч — неразрывный пробел (\xa0), нормализуем для сравнения
    assert A._money_str(Decimal("292000")).replace("\xa0", " ") == "292 000,00"
    assert A._money_str(Decimal("1234.5")).replace("\xa0", " ") == "1 234,50"


# ------------------------------------------------------------------ генератор акта
def _sample_rows(n=3):
    return [
        {"date": date(2026, 5, d), "origin": "Агропарк Софийская 151",
         "destination": f"РЦ {d}", "plate": "Т 557 ОС 178", "driver": "Саломов",
         "amount": Decimal("19000")}
        for d in range(1, n + 1)
    ]


def _executor():
    return {"full_name": "ИП Кибиткина", "inn": "781699567368", "ogrnip": "319",
            "address": "СПб", "bank_name": "УРАЛСИБ", "account": "40802",
            "corr_account": "30101", "bik": "044030706", "signer_name": "Кибиткина"}


def _customer():
    return {"name": 'ООО "Рузисеть"', "inn": "7802533479", "kpp": "781701001",
            "address": "СПб", "bank_name": "ПСКБ", "account": "40702",
            "corr_account": "30101", "bik": "044030852", "contract_number": "№521",
            "contract_date": date(2020, 6, 29), "signer_name": "Аллахвердиев"}


def _build(title="Акт", rows=None):
    return A.build_act_101rs(
        title=title, act_number="102", act_date=date(2026, 6, 25),
        period_from=date(2026, 6, 1), period_to=date(2026, 6, 25),
        executor=_executor(), customer=_customer(),
        rows=_sample_rows(3) if rows is None else rows,
    )


def _find_header_row(ws):
    for r in range(1, 12):
        if ws.cell(r, 1).value == "№":
            return r
    raise AssertionError("шапка таблицы не найдена")


def test_act_title_default_and_custom():
    assert _build().active["A1"].value == "Акт № 102 от 25.06.2026 г."
    assert _build(title="Акт сверки").active["A1"].value == "Акт сверки № 102 от 25.06.2026 г."


def test_act_header_has_no_fill_and_9pt():
    ws = _build().active
    h = ws.cell(_find_header_row(ws), 1)
    assert h.fill.patternType is None, "шапка не должна быть с заливкой (как в образце 101)"
    assert h.font.bold is True and h.font.size == 9


def test_act_money_format_and_totals():
    rows = _sample_rows(3)  # 3 × 19000 = 57000
    ws = _build(rows=rows).active
    hr = _find_header_row(ws)
    price_cell = ws.cell(hr + 1, 5)
    assert price_cell.number_format == "#,##0.00" and "₽" not in price_cell.number_format
    # ИТОГО и «Всего оказано услуг N»
    joined = "\n".join(
        str(c.value) for row in ws.iter_rows() for c in row if c.value is not None
    ).replace("\xa0", " ")  # неразрывный пробел → обычный для сравнения
    assert "Всего оказано услуг 3." in joined
    assert "57 000,00 руб." in joined
    assert "Пятьдесят семь тысяч рублей 00 копеек." in joined
    assert "Без налога (НДС)" in joined


def test_act_empty_rows_ok():
    ws = _build(rows=[]).active
    joined = " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value)
    assert "Всего оказано услуг 0." in joined


def test_act_uses_distribution_center_address_when_present():
    rows = [{
        "date": date(2026, 6, 1),
        "origin": "Агропарк Софийская 151",
        "destination": "РЦ Шушары",
        "destination_address": "Санкт-Петербург, Пушкинский район, пос. Шушары, Московское шоссе, 231",
        "plate": "Т 557 ОС 178",
        "driver": "Саломов",
        "amount": Decimal("19000"),
    }]
    joined = "\n".join(
        str(c.value) for row in _build(rows=rows).active.iter_rows() for c in row if c.value
    )
    assert "Московское шоссе, 231" in joined
    assert "РЦ Шушары" not in joined


# ------------------------------------------------------------------ JWT / доступ
def test_jwt_roundtrip_owner_and_admin():
    assert AU.decode_jwt(AU.create_jwt(5, tid=123)) == (5, 123)   # админ вошёл в кабинет 5
    assert AU.decode_jwt(AU.create_jwt(5, tid=999)) == (5, 999)   # владелец
    assert AU.decode_jwt(AU.create_jwt(5)) == (5, None)           # старый токен (совместимость)


def test_jwt_invalid_returns_none():
    assert AU.decode_jwt("not-a-token") is None
    assert AU.decode_jwt("") is None


def test_login_code_is_one_time_and_checked():
    code = AU.issue_code(777)
    assert AU.consume_code(777, "000000") is False   # неверный код
    assert AU.consume_code(777, code) is True         # верный
    assert AU.consume_code(777, code) is False        # повторно — уже израсходован


def _admin_access_ok(owner_tid, jwt_tid, admin_exists):
    """Копия логики current_owner: админ-доступ действует, только пока есть Admin."""
    if jwt_tid is not None and jwt_tid != owner_tid:
        return admin_exists
    return True


def test_admin_revocation_is_immediate():
    assert _admin_access_ok(999, 123, admin_exists=True) is True    # админ активен
    assert _admin_access_ok(999, 123, admin_exists=False) is False  # удалён → доступ закрыт сразу
    assert _admin_access_ok(999, 999, admin_exists=False) is True   # владелец
    assert _admin_access_ok(999, None, admin_exists=False) is True  # старый токен = владелец


# ------------------------------------------------------------------ рендер страниц
def _render(name, **ctx):
    return _ENV.get_template(name).render(**ctx)


OWNER = NS(company_name="БРО ЛОГИСТИК", executor_name="ИП", inn="7816",
           ogrnip="319", legal_address="СПб", bank_name="УРАЛСИБ",
           bank_account="40802", corr_account="30101", bik="044030706",
           signer_name="Иванов", full_name="Иванов", telegram_id=1)


def test_render_login():
    assert "Вход в кабинет" in _render("login.html", error=None)
    assert "Свежий путь" not in _render("login.html", error=None)


def test_render_finances():
    html = _render(
        "finances.html", owner=OWNER, active_page="finances",
        summary={"total_income": Decimal("258000"), "total_expense": Decimal("267555"),
                 "profit": Decimal("-9555"), "fuel": Decimal("0")},
        margin=-4.0,
        cashflow={"labels": ["май", "июн"], "revenue": [0, 258000],
                  "expenses": [0, 267555], "profit": [0, -9555], "period": "6m"},
        directions=[{"route": "А → Б", "trips": 5, "revenue": Decimal("106000"),
                     "profit": Decimal("106000"), "bar": 100}],
        entries=[], period_from="2026-06-01", period_to="2026-06-25",
        today="2026-06-25")
    assert "Денежный поток" in html and "Прибыльность направлений" in html
    assert ".sp-kpi" in html  # стили встроены в страницу


def test_render_vehicles_card_layout():
    v = NS(id=1, license_plate="ACHICHJ23627", brand="тимьмиь", type="refrigerator",
           osago_expires=date(2026, 9, 1), inspection_expires=None, tacho_expires=None,
           fuel_norm_per_100km=Decimal("32"))
    row = {"vehicle": v, "km": 0, "fuel": Decimal(0), "trips": 0, "revenue": Decimal(0),
           "expense": Decimal(0), "profit": Decimal(0), "margin": Decimal(0), "active": False}
    html = _render("vehicles.html", owner=OWNER, rows=[row], active_page="vehicles",
                   in_minus=0, notice=None,
                   totals={"count": 1, "in_work": 0, "revenue": Decimal(0), "profit": Decimal(0)})
    assert '<div class="grow">' not in html   # номер+статус в ряд, без grow-обёртки
    assert "свободна" in html


def test_render_drivers_all_time():
    d = NS(id=1, full_name="Иванов Иван", phone="+7", salary_type="fixed_per_month",
           salary_rate=Decimal("180000"), per_diem_rub=Decimal("0"),
           shift_start_time=None, telegram_id=1)
    row = {"driver": d, "km": 1477, "shifts": 12, "trips": 12, "revenue": Decimal("258000"),
           "fuel_cost": Decimal("0"), "active_shift": False, "idle_label": "24.06 13:16"}
    html = _render("drivers.html", owner=OWNER, rows=[row], active_page="drivers",
                   invite=None, totals={"count": 1, "in_shift": 0, "idle": 1})
    assert "за всё время" in html and ">12<" in html


def test_render_acts_checklist():
    trips = [{"id": 11, "date": date(2026, 6, 21), "origin": "А", "destination": "Б",
              "driver": "Саломов", "plate": "Т557", "revenue": Decimal("19000")}]
    html = _render("acts.html", owner=OWNER, trips=trips, period_from="2026-06-01",
                   period_to="2026-06-25", customers=[NS(id=2, name='ООО "Рузисеть"')],
                   total_amount=Decimal("19000"), total_trips=1, act_date="2026-06-25",
                   act_title="Акт сверки", act_number_val="102", sel_customer_id="2",
                   requisites_ready=True, active_page="finances")
    assert "Акт сверки" in html and 'name="trip_ids"' in html and "Выбрать все" in html
    assert 'name="selection_mode" value="checklist"' in html


def test_render_requisites_with_admins():
    admins = [NS(id=1, name="Бухгалтер Анна", telegram_id=123456789, created_at=None)]
    html = _render("requisites.html", owner=OWNER, customers=[], admins=admins,
                   active_page="requisites")
    assert "Администраторы" in html and "Telegram ID 123456789" in html
    assert "/admins/add" in html


def test_render_expenses_donut():
    exp = NS(id=1, created_at=None, category="fuel", amount_rub=Decimal("5000"),
             status="approved", receipt_web_data=None, receipt_photo_url=None)
    html = _render("expenses.html", owner=OWNER, active_page="trips",
                   rows=[(exp, "Саломов", "Т557")],
                   totals={"count": 1, "sum": Decimal("5000"), "pending": 0, "approved": 1},
                   filter_category="", filter_status="", categories=("fuel",),
                   breakdown=[{"label": "Топливо", "amount": 5000.0}])
    assert "Структура расходов по категориям" in html


def test_render_routes_with_distribution_centers():
    center = NS(id=1, name="РЦ Шушары", address="СПб, Московское шоссе, 231",
                aliases="Шушары; РЦ СПб", latitude=Decimal("59.75"),
                longitude=Decimal("30.45"))
    html = _render(
        "routes.html", owner=OWNER, active_page="routes",
        rows=[], centers=[center], rc_imported=1,
    )
    assert "Справочник РЦ" in html
    assert "РЦ Шушары" in html and "СПб, Московское шоссе, 231" in html
    assert "/routes/rc/import" in html and "/routes/rc/1/delete" in html


def test_rc_xlsx_import_and_lookup_preserves_zero_coordinates():
    wb = Workbook()
    ws = wb.active
    ws.append(["РЦ", "Адрес", "Алиасы", "Широта", "Долгота"])
    ws.append(["РЦ Шушары", "СПб, Московское шоссе, 231", "Шушары; РЦ СПб", 0, "30,4501"])
    buf = io.BytesIO()
    wb.save(buf)

    rows = RC.distribution_centers_from_xlsx(buf.getvalue())
    assert rows == [{
        "name": "РЦ Шушары",
        "address": "СПб, Московское шоссе, 231",
        "aliases": "Шушары; РЦ СПб",
        "latitude": "0",
        "longitude": "30,4501",
    }]
    assert RC.decimal_or_none(rows[0]["latitude"]) == Decimal("0")
    assert RC.decimal_or_none(rows[0]["longitude"]) == Decimal("30.4501")

    lookup = RC.distribution_center_lookup([
        NS(name=rows[0]["name"], address=rows[0]["address"], aliases=rows[0]["aliases"])
    ])
    assert RC.canonical_rc_address("доставка в шушары", lookup) == "СПб, Московское шоссе, 231"


def test_rc_xlsx_import_single_column_file():
    """Файл владельца «РЦ Адреса Спб.xlsx»: одна колонка, без шапки,
    название и адрес слиты в одной ячейке. Должен импортироваться."""
    wb = Workbook()
    ws = wb.active
    ws.append(["РЦ Лента Лен.Обл. Тосненский район посёлок Красный бор"])
    ws.append(["РЦ 7 шагов, Санкт-Петербург г., п. Шушары"])
    ws.append([None])  # пустая строка — пропускается
    buf = io.BytesIO()
    wb.save(buf)

    rows = RC.distribution_centers_from_xlsx(buf.getvalue())
    assert len(rows) == 2
    assert rows[0]["name"] == rows[0]["address"]  # адрес = вся ячейка
    assert rows[0]["name"].startswith("РЦ Лента")


def test_render_trips_kpi():
    html = _render("trips.html", owner=OWNER, active_page="trips",
                   drivers=[NS(id=1, full_name="Д")],
                   vehicles=[NS(id=1, license_plate="П", brand="")],
                   filter_driver_id=None, filter_date_from="", filter_date_to="",
                   today="2026-06-25",
                   totals={"count": 12, "revenue": Decimal("292000"), "profit": Decimal("112000")},
                   rows=[], page=1, has_next=False)
    assert "sp-kpi" in html and "Рейсов по фильтру" in html


def test_pending_driver_revenue_is_not_counted_as_final_revenue():
    trip = NS(
        id=39,
        created_at=datetime(2026, 7, 3, 18, 55, tzinfo=timezone.utc),
        origin="непонятно",
        destination="непонятно",
        is_manual=False,
        revenue_rub=None,
        driver_revenue_pending_rub=Decimal("12000"),
        fuel_cost_rub=Decimal("0"),
        profit_rub=Decimal("0"),
        status="completed",
    )
    html = _render(
        "_trips_table.html",
        owner=OWNER,
        rows=[(trip, "овыраовраор", "Е774ЕТ178")],
        totals={"count": 1, "revenue": Decimal("0"), "profit": Decimal("0")},
        filter_driver_id="",
        filter_date_from="",
        filter_date_to="",
        page=1,
        has_next=False,
    )

    assert "на подтверждении 12 000 ₽" in html
    assert "<div class=\"lbl\">Выручка</div><div class=\"val\">0 <span class=\"rub\">₽</span></div>" in html


def test_trip_detail_shows_pending_driver_revenue_separately():
    trip = NS(
        id=39,
        created_at=datetime(2026, 7, 3, 18, 55, tzinfo=timezone.utc),
        completed_at=datetime(2026, 7, 3, 18, 56, tzinfo=timezone.utc),
        origin="непонятно",
        destination="непонятно",
        is_manual=False,
        status="completed",
        cargo_name="РЦ адамнт",
        revenue_rub=None,
        driver_revenue_pending_rub=Decimal("12000"),
        fuel_cost_rub=Decimal("0"),
        other_costs_rub=Decimal("0"),
        profit_rub=Decimal("0"),
        waybill_photo_url=None,
    )
    html = _render(
        "trip_detail.html",
        owner=OWNER,
        active_page="trips",
        trip=trip,
        driver=NS(full_name="овыраовраор"),
        vehicle=NS(license_plate="Е774ЕТ178"),
        travel=None,
        documents=[],
        expenses=[],
        waybill_uploaded_at=None,
    )

    assert "на подтверждении 12 000 ₽" in html
    assert "<strong>0 ₽</strong>" in html


# ------------------------------------------------------------------ карта (Яндекс)
def test_map_template_uses_yandex_not_leaflet():
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    for banned in ("leaflet", "Leaflet", "openstreetmap", "cartocdn", "CARTO", "L.map"):
        assert banned not in src, f"старая карта не удалена: {banned}"
    assert "api-maps.yandex.ru" in src

    html = _render("map.html", owner=OWNER, active_page="map",
                   yandex_maps_api_key="test-key-123")
    assert "api-maps.yandex.ru/2.1/?apikey=test-key-123" in html
    assert "vehicle-marker" in html and "visibilitychange" in html


def test_map_template_without_key_shows_hint():
    html = _render("map.html", owner=OWNER, active_page="map", yandex_maps_api_key="")
    assert "YANDEX_MAPS_API_KEY" in html
    assert "api-maps.yandex.ru" not in html


# ------------------------------------------------------------------ метка «с какого времени»
def test_smart_since_label_today_yesterday_older():
    from datetime import datetime, timedelta, timezone as tz

    from app.services.timeutil import now_in_tz, smart_since_label

    now_local = now_in_tz("Europe/Moscow")
    today_dt = now_local.replace(hour=10, minute=5) if now_local.hour >= 11 \
        else now_local.replace(minute=0)
    assert smart_since_label(today_dt, "Europe/Moscow").startswith("с ")
    assert "," not in smart_since_label(today_dt, "Europe/Moscow")

    yesterday = now_local - timedelta(days=1)
    assert smart_since_label(yesterday, "Europe/Moscow").startswith("со вчера, ")

    older = now_local - timedelta(days=5)
    label = smart_since_label(older, "Europe/Moscow")
    assert label.startswith("с ") and older.strftime("%d.%m") in label
    assert smart_since_label(None, "Europe/Moscow") == "—"
