"""Счётчик попыток входа — по настоящему адресу, а не по адресу прокси.

PROBLEMS №40: за прокси Railway у всех запросов один адрес. Счётчик по нему
был общим, и 15 неверных кодов с одного телефона закрывали вход водителей
всем на 5 минут — это можно было делать нарочно (проверка 26.09.2026).
"""
import os

os.environ.setdefault("OWNER_BOT_TOKEN", "test")
os.environ.setdefault("DRIVER_BOT_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("JWT_SECRET", "test-secret-please-set-a-long-one-in-prod")

from starlette.requests import Request  # noqa: E402

from app.web import router as R  # noqa: E402


def _request(peer: str, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({
        "type": "http", "method": "POST", "path": "/api/driver/redeem",
        "headers": headers, "client": (peer, 5000),
    })


def test_за_прокси_берём_адрес_клиента_из_заголовка_прокси():
    # Railway: соединение от прокси из внутренней сети; адрес клиента —
    # первый в цепочке, дальше прокси дописывает свои звенья.
    assert R._trusted_client_ip(_request("100.64.0.7", "95.24.1.10")) == "95.24.1.10"
    assert R._trusted_client_ip(_request("10.0.0.3", "95.24.1.10, 100.64.9.9")) == "95.24.1.10"


def test_напрямую_из_интернета_заголовку_не_верим():
    # Клиент сам написал X-Forwarded-For, чтобы обойти счётчик.
    assert R._trusted_client_ip(_request("95.24.1.10", "8.8.8.8")) == "95.24.1.10"


def test_без_заголовка_адрес_соединения():
    assert R._trusted_client_ip(_request("95.24.1.10")) == "95.24.1.10"


def test_чужой_перебор_не_закрывает_вход_другим():
    R._REDEEM_HITS.clear()
    attacker = R._trusted_client_ip(_request("100.64.0.7", "203.0.113.5"))
    driver = R._trusted_client_ip(_request("100.64.0.7", "95.24.1.10"))
    for _ in range(R._REDEEM_MAX_PER_WINDOW):
        R._redeem_rate_ok(attacker)
    assert R._redeem_rate_ok(attacker) is False
    assert R._redeem_rate_ok(driver) is True
    R._REDEEM_HITS.clear()


def test_огромный_запрос_на_вход_водителя_не_читается_целиком():
    """Адрес входа водителя открыт без пароля — тело больше 64 КБ отвергаем,
    не читая в память (проверка безопасности 26.09.2026)."""
    import asyncio

    import pytest
    from fastapi import HTTPException

    def request(body: bytes, declared: bool) -> Request:
        chunks = [body[i:i + 8192] for i in range(0, len(body), 8192)] or [b""]
        sent = iter(chunks)

        async def receive():
            try:
                part = next(sent)
                return {"type": "http.request", "body": part, "more_body": True}
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        headers = [(b"content-length", str(len(body)).encode())] if declared else []
        return Request({"type": "http", "method": "POST", "path": "/api/driver/redeem",
                        "headers": headers}, receive)

    big = b'{"code": "' + b"A" * 200_000 + b'"}'
    for declared in (True, False):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(R._json_body(request(big, declared)))
        assert caught.value.status_code == 413
    ok = asyncio.run(R._json_body(request(b'{"code": "ABCDEFGH"}', True)))
    assert ok == {"code": "ABCDEFGH"}


def test_телефон_водителя_не_засыпет_сервер_действиями():
    R._DRIVER_ACTION_HITS.clear()
    for _ in range(R._DRIVER_ACTIONS_MAX):
        assert R._driver_rate_ok(R._DRIVER_ACTION_HITS, 7, R._DRIVER_ACTIONS_MAX)
    assert R._driver_rate_ok(R._DRIVER_ACTION_HITS, 7, R._DRIVER_ACTIONS_MAX) is False
    # Соседний телефон этим не задет.
    assert R._driver_rate_ok(R._DRIVER_ACTION_HITS, 8, R._DRIVER_ACTIONS_MAX) is True
    assert R._driver_slow_down().status_code == 429
    R._DRIVER_ACTION_HITS.clear()
