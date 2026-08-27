"""Разбор чека из ТЕКСТА — для OCR, который отдаёт текст, а не готовый JSON.

LlamaParse (10 000 страниц в месяц бесплатно, ограничения в 1 МБ нет) не
понимает вопрос «сколько тут итого» — он просто распознаёт буквы. Значит сумму
и литры достаём мы, и здесь это проверяется на настоящих формах чеков АЗС.

Правило, которое эти тесты закрепляют: **лучше не распознать, чем распознать
неправильно.** Ошибка в сумме молча искажает расходы и зарплату водителя;
пустой ответ просто вернёт бота к ручному вводу, как было всегда.
"""
import os
from decimal import Decimal

import pytest

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test")

from app.services.receipt_ocr import (  # noqa: E402
    parse_odometer_text,
    parse_receipt_text,
)

LUKOIL = """
ООО "ЛУКОЙЛ-Северо-Западнефтепродукт"
АЗС № 241  Санкт-Петербург, Софийская ул., 60
ИНН 7825439514
КАССОВЫЙ ЧЕК
ДТ-Евро                 42,50 Л
Цена                    64,90
Сумма                 2758,25
ИТОГО                 2758,25
БЕЗНАЛИЧНЫМИ          2758,25
Сумма НДС 20%          459,71
СМЕНА 12  ЧЕК 0041
"""

GAZPROM = """
# Газпромнефть
АЗС 178
ОПТИ ДИЗЕЛЬ
Объем, л            60,00
Цена за литр        66,15
К оплате          3 969,00
НДС 20%             661,50
Сдача                 0,00
"""

STOLBIK = """
ИТОГ
4 250.00
Карта **** 1234
"""

DEFIS = """
АИ-95
Отпущено 30,00 л
ИТОГО К ОПЛАТЕ 1950-00
"""


def test_lukoil_receipt():
    r = parse_receipt_text(LUKOIL)
    assert r is not None
    assert r.amount_rub == Decimal("2758.25")
    assert r.liters == Decimal("42.50")


def test_gazprom_receipt_with_markdown_and_thousands_space():
    r = parse_receipt_text(GAZPROM)
    assert r is not None
    assert r.amount_rub == Decimal("3969.00")
    assert r.liters == Decimal("60.00")


def test_amount_may_stand_on_the_next_line():
    r = parse_receipt_text(STOLBIK)
    assert r is not None and r.amount_rub == Decimal("4250.00")


def test_dash_means_kopecks_not_a_range():
    """«1950-00» на чеке — это 1950 рублей 00 копеек."""
    r = parse_receipt_text(DEFIS)
    assert r is not None and r.amount_rub == Decimal("1950.00")
    assert r.liters == Decimal("30.00")


def test_vat_price_and_change_are_never_taken_for_the_total():
    """Самая дорогая ошибка: принять НДС, цену литра или сдачу за сумму чека."""
    r = parse_receipt_text(LUKOIL)
    assert r.amount_rub != Decimal("459.71")   # НДС
    assert r.amount_rub != Decimal("64.90")    # цена за литр
    r2 = parse_receipt_text(GAZPROM)
    assert r2.amount_rub != Decimal("661.50")  # НДС
    assert r2.amount_rub != Decimal("0.00")    # сдача


def test_price_per_litre_is_not_taken_for_volume():
    r = parse_receipt_text(LUKOIL)
    assert r.liters != Decimal("64.90")


def test_garbage_returns_nothing_instead_of_guessing():
    for bad in (None, "", "   ", "фотография размыта", "ИНН 7825439514"):
        assert parse_receipt_text(bad) is None


def test_implausible_amounts_are_rejected():
    assert parse_receipt_text("ИТОГО 0,00") is None
    assert parse_receipt_text("ИТОГО 12345678") is None


def test_odometer_takes_the_longest_number():
    text = "12:45  95 км/ч  ODO 154820 km  t 21C"
    r = parse_odometer_text(text)
    assert r is not None and r.km == 154820


def test_odometer_ignores_short_numbers():
    assert parse_odometer_text("12:45 95 21") is None


# ------------------------------------------------------------------ провайдер
def test_llamaparse_is_wired_as_a_text_provider():
    """Текстовый провайдер обязан идти по своей ветке, а не через промпт.

    Если забыть про _TEXT_PROVIDERS, код попросит LlamaParse вернуть JSON —
    а он так не умеет, и OCR будет молча всегда отдавать «не распознал».
    """
    from app.services import receipt_ocr as R

    assert "llamaparse" in R._TEXT_PROVIDERS
    src = open("app/services/receipt_ocr.py", encoding="utf-8").read()
    assert "if provider in _TEXT_PROVIDERS:" in src
    assert src.count("if provider in _TEXT_PROVIDERS:") == 2, "и чек, и одометр"
    # ключ читается из настроек, а не зашит в код
    assert 'getattr(settings, "llama_cloud_api_key", "")' in src
    # настоящий ключ в коде: префикс плюс длинный хвост. Сам по себе префикс
    # разрешён — по нему мы узнаём ключ, вписанный не в ту переменную.
    import re as _re
    assert not _re.search(r"llx-[a-z0-9]{20,}", src), "ключ не должен попасть в репозиторий"


def test_ocr_is_off_without_a_key():
    """Без ключа OCR обязан молчать, а бот — спрашивать сумму вручную."""
    from app.config import settings
    from app.services import receipt_ocr as R

    old_provider, old_key = settings.receipt_ocr_provider, settings.llama_cloud_api_key
    try:
        settings.receipt_ocr_provider = "llamaparse"
        settings.llama_cloud_api_key = ""
        assert R.is_enabled() is False
        settings.llama_cloud_api_key = "llx-тестовый"
        assert R.is_enabled() is True
    finally:
        settings.receipt_ocr_provider, settings.llama_cloud_api_key = old_provider, old_key


def test_total_wins_over_a_position_line():
    """В чеке несколько «сумм»: у позиции, у НДС и итог. Берём итог."""
    text = """
    ДТ 20,00 Л
    Сумма позиции 1200,00
    Сумма НДС 240,00
    ИТОГО К ОПЛАТЕ 1440,00
    """
    r = parse_receipt_text(text)
    assert r.amount_rub == Decimal("1440.00")


def test_reading_keeps_raw_text_for_checking():
    """Сырой текст сохраняем — по нему видно, почему распознало именно так."""
    r = parse_receipt_text(LUKOIL)
    assert r.raw and "ИТОГО" in r.raw.upper()


# ------------------------------------------------------------------ QR чека
# В QR фискального чека лежит не ссылка, а строка данных ФНС, и сумма в ней
# уже точная. Поэтому она всегда важнее того, что удалось прочитать глазами.
from app.services.receipt_ocr import parse_fiscal_qr  # noqa: E402

QR = "t=20260827T1230&s=2758.25&fn=9960440301234567&i=12345&fp=1234567890&n=1"


def test_qr_gives_the_exact_amount():
    r = parse_fiscal_qr(QR)
    assert r is not None and r.amount_rub == Decimal("2758.25")


def test_qr_wins_over_the_recognised_text():
    """Если в чеке есть и QR, и текст — верим QR: там нечему ошибиться."""
    text = QR + "\nИТОГО 2 758,52\nДТ 42,50 Л"   # в тексте цифры переставлены
    r = parse_receipt_text(text)
    assert r.amount_rub == Decimal("2758.25"), "взяли текст вместо QR"
    # литров в QR нет — их всё равно берём из текста
    assert r.liters == Decimal("42.50")


def test_random_text_with_s_is_not_a_receipt():
    """«s=» без реквизитов фискального документа — просто совпадение."""
    assert parse_fiscal_qr("https://example.com/?s=500") is None
    assert parse_fiscal_qr("s=2758.25") is None


def test_qr_with_implausible_amount_is_rejected():
    assert parse_fiscal_qr("t=2026&s=0&fn=996044&i=1&fp=1") is None
    assert parse_fiscal_qr("t=2026&s=9999999&fn=996044&i=1&fp=1") is None


def test_qr_survives_being_pasted_with_spaces_around():
    r = parse_fiscal_qr("  Чек: " + QR + "  ")
    assert r is not None and r.amount_rub == Decimal("2758.25")


def test_startup_log_says_whether_ocr_is_alive():
    """При старте в лог обязано попасть состояние OCR.

    Логи Railway 26.08 показали ровно эту дыру: OCR молчал, и по логу было
    не понять — он выключен, не тот провайдер, нет ключа или он сломался.
    Теперь это видно первой же строкой, без отправки фото чека.
    """
    from app.services.receipt_ocr import provider_status

    src = open("app/main.py", encoding="utf-8").read()
    assert "_ocr.provider_status()" in src
    line = provider_status()
    assert line.startswith("OCR чека: провайдер=")
    assert "ключ=" in line and "флаг=" in line


def test_log_never_prints_a_secret_even_if_it_lands_in_the_wrong_variable():
    """Ключ не должен попасть в лог, куда бы его ни вписали.

    26.08.2026 ключ LlamaParse оказался в RECEIPT_OCR_PROVIDER вместо имени
    провайдера, и строка состояния напечатала его в лог Railway целиком.
    Логи выгружают и пересылают — значит секрет утёк. Больше чужое значение
    этой переменной в лог не попадает никогда.
    """
    from app.config import settings
    from app.services.receipt_ocr import provider_status

    secret = "llx-bl5fe7o4nq7z5lij55qqhpn71m5battjffol2fcrptcbzs1y"
    old = settings.receipt_ocr_provider
    try:
        settings.receipt_ocr_provider = secret
        line = provider_status()
        assert secret not in line, "ключ утёк в лог"
        assert "ВПИСАН КЛЮЧ" in line, "надо прямо сказать, в чём ошибка"

        # и любое другое незнакомое значение тоже не печатаем
        settings.receipt_ocr_provider = "какая-то-секретная-строка"
        line = provider_status()
        assert "какая-то-секретная-строка" not in line
        assert "значение скрыто" in line
    finally:
        settings.receipt_ocr_provider = old


def test_known_provider_names_are_printed_as_is():
    """У правильного имени скрывать нечего — его видно в логе."""
    from app.config import settings
    from app.services.receipt_ocr import provider_status

    old = settings.receipt_ocr_provider
    try:
        settings.receipt_ocr_provider = "llamaparse"
        assert "провайдер=llamaparse" in provider_status()
    finally:
        settings.receipt_ocr_provider = old


# ------------------------------------------------- разбор ответа LlamaParse
def test_result_is_found_whatever_the_field_is_called():
    """Текст забираем из любого известного поля ответа.

    В официальном примере из кабинета это `markdown_full`, в REST-документации
    `markdown`. Завязаться на одно имя — значит однажды молча получать пустоту
    вместо распознанного чека.
    """
    from app.services.receipt_ocr import _collect_text

    assert _collect_text({"markdown_full": "ИТОГО 100,00"}) == ["ИТОГО 100,00"]
    assert _collect_text({"markdown": "ИТОГО 100,00"}) == ["ИТОГО 100,00"]
    # ответ бывает вложенным: страницы внутри задания
    nested = {"job": {"result": {"pages": [{"markdown": "стр1"}, {"text": "стр2"}]}}}
    assert _collect_text(nested) == ["стр1", "стр2"]
    # пустое и не-текстовое игнорируем
    assert _collect_text({"markdown": "   ", "id": 5, "status": "SUCCESS"}) == []


def test_failed_request_is_logged_with_the_body(caplog):
    """При ошибке в лог обязано попасть ТЕЛО ответа, а не только код.

    Проверить вызов вживую нечем — ключ владельца я не беру. Значит первая же
    неудачная отправка чека должна сама объяснить, что API не понравилось.
    """
    import logging as _logging

    from app.services.receipt_ocr import _ok

    with caplog.at_level(_logging.WARNING):
        assert _ok(422, '{"detail":"configuration field required"}', "загрузка файла") is False
    assert "configuration field required" in caplog.text
    assert "422" in caplog.text
    assert _ok(200, "{}", "загрузка файла") is True


def test_ocr_uses_only_libraries_that_are_actually_installed():
    """Каждая сторонняя библиотека модуля обязана быть в requirements.txt.

    27.08.2026 OCR падал на `import httpx` при каждом вызове: в комментарии
    было написано, что httpx приходит вместе с aiogram, а приходит aiohttp.
    Полгода это не всплывало, потому что ключей не было и до вызова не
    доходило. Теперь такое ловится тестом, а не логом с боевого сервера.
    """
    import ast
    import sys

    src = open("app/services/receipt_ocr.py", encoding="utf-8").read()
    requirements = open("requirements.txt", encoding="utf-8").read().lower()

    modules = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])

    for name in sorted(modules):
        if name == "app" or name in sys.stdlib_module_names:
            continue
        assert name.lower() in requirements, (
            f"{name} импортируется, но его нет в requirements.txt — "
            "на сервере вызов упадёт на импорте"
        )


# ------------------------------------------------- поведение в боте
def test_ocr_never_silently_replaces_the_amount_the_driver_typed():
    """Распознанная сумма НЕ подменяет введённую водителем.

    Раньше подменяла молча: OCR ошибся — расход уехал с неверной цифрой, и об
    этом никто не узнавал. Теперь расхождение уходит владельцу строкой, а
    решает он. То же правило, что и в разборе текста: лучше не распознать,
    чем распознать неправильно.
    """
    src = open("app/bots/driver_bot.py", encoding="utf-8").read()
    start = src.index("async def expense_receipt_photo")
    body = src[start:start + 2500]
    assert 'state.update_data(amount=' not in body, "OCR снова подменяет сумму водителя"
    assert "На чеке распознано" in body, "расхождение должно уходить владельцу"
    assert "extra_note=ocr_note" in body
    # и это должно быть видно в логах Railway, а не в debug
    assert "logger.info(" in body
    assert "logger.debug(" not in body, "ошибку OCR не прячем в debug — её не видно в Railway"


def test_expense_note_reaches_the_owner_caption():
    """Строка про чек должна дойти до сообщения владельцу, а не потеряться."""
    src = open("app/bots/driver_bot.py", encoding="utf-8").read()
    start = src.index("async def _finalize_expense")
    body = src[start:start + 4000]
    assert "extra_note: str | None = None" in body
    assert 'caption += f"\\n{extra_note}"' in body


def test_upload_always_sends_the_configuration_field():
    """Без поля configuration загрузка отвечает HTTP 400.

    Боевой ответ 27.08.2026:
    «configuration field is required for multipart requests.»
    Поле обязательное — и по одной попытке этого было не видно, потому что
    в примерах документации оно выглядит необязательным.
    """
    from app.services.receipt_ocr import _LLAMA_CONFIGS

    src = open("app/services/receipt_ocr.py", encoding="utf-8").read()
    start = src.index("async def _llamaparse")
    body = src[start:start + 2000]
    assert 'form.add_field("configuration", configuration)' in body
    # запасная настройка нужна: если пустую не примут, вторая заведомо валидна
    assert len(_LLAMA_CONFIGS) >= 2
    assert _LLAMA_CONFIGS[0] == "{}", "сначала пробуем тариф по умолчанию — он дешевле"
    assert "agentic" in _LLAMA_CONFIGS[1], "запасная — та, что показана в кабинете"
    # каждая попытка собирает FormData заново: она одноразовая
    assert body.count("aiohttp.FormData()") == 1
    assert "for attempt, configuration in enumerate(_LLAMA_CONFIGS" in body
