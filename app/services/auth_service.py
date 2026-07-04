"""
Авторизация владельца в веб-кабинете.

Сценарий:
  1) Владелец в боте отправляет /login.
     -> Бот генерирует 6-значный код, сохраняет в памяти (telegram_id -> code)
        и отсылает код в чат.
  2) Владелец заходит на /login веб-кабинета, вводит telegram_id и код.
     -> consume_code() проверяет код, выдаёт JWT, кладёт в httpOnly cookie.

In-memory dict — норм для MVP. При перезапуске процесса все коды теряются,
но они и так живут 5 минут. JWT в cookie уже выданные при этом не сгорают —
их подписывает JWT_SECRET, и они валидны до собственного истечения.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings

logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=5)
JWT_TTL = timedelta(days=7)
JWT_ALGO = "HS256"


@dataclass
class CodeEntry:
    code: str
    expires_at: datetime


# telegram_id -> CodeEntry
_login_codes: dict[int, CodeEntry] = {}


def issue_code(telegram_id: int) -> str:
    """Сгенерировать (или перевыпустить) 6-значный код для владельца."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    _login_codes[telegram_id] = CodeEntry(
        code=code, expires_at=datetime.now(timezone.utc) + CODE_TTL
    )
    return code


def consume_code(telegram_id: int, code: str) -> bool:
    """Проверить код. На успехе удалить, чтобы был одноразовым."""
    entry = _login_codes.get(telegram_id)
    if entry is None:
        return False
    if entry.expires_at < datetime.now(timezone.utc):
        _login_codes.pop(telegram_id, None)
        return False
    if entry.code != code.strip():
        return False
    _login_codes.pop(telegram_id, None)
    return True


def create_jwt(owner_id: int, tid: int | None = None) -> str:
    """JWT для доступа к кабинету owner_id. tid — telegram_id того, кто вошёл
    (владелец или админ); нужен, чтобы уметь мгновенно отзывать доступ админа."""
    payload = {
        "sub": str(owner_id),
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + JWT_TTL,
    }
    if tid is not None:
        payload["tid"] = int(tid)
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGO)


def decode_jwt(token: str) -> tuple[int, int | None] | None:
    """Вернуть (owner_id, tid) или None при ошибке/истечении.
    tid = None для старых токенов (до ввода админов) — трактуем как владельца."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGO])
        owner_id = int(payload["sub"])
        raw_tid = payload.get("tid")
        return owner_id, (int(raw_tid) if raw_tid is not None else None)
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        logger.debug("JWT decode failed: %s", exc)
        return None


# =====================================================================
# Постоянные сессии (таблица web_sessions): вход живёт, пока его не
# завершат. В cookie — случайный токен, в БД — только SHA-256 от него.
# =====================================================================
SESSION_COOKIE = "session"
SESSION_COOKIE_MAX_AGE = 10 * 365 * 24 * 3600  # «навсегда» (10 лет)


def new_session_token() -> str:
    return secrets.token_urlsafe(48)


def session_token_hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


def device_label_from_user_agent(user_agent: str | None) -> str:
    """Короткая подпись устройства для списка «Устройства»: «Chrome · Windows»."""
    ua = (user_agent or "").lower()
    if "edg/" in ua or "edge" in ua:
        browser = "Edge"
    elif "opr/" in ua or "opera" in ua:
        browser = "Opera"
    elif "yabrowser" in ua:
        browser = "Яндекс Браузер"
    elif "firefox" in ua:
        browser = "Firefox"
    elif "chrome" in ua:
        browser = "Chrome"
    elif "safari" in ua:
        browser = "Safari"
    else:
        browser = "Браузер"
    if "iphone" in ua:
        os_name = "iPhone"
    elif "ipad" in ua:
        os_name = "iPad"
    elif "android" in ua:
        os_name = "Android"
    elif "mac os" in ua or "macintosh" in ua:
        os_name = "macOS"
    elif "windows" in ua:
        os_name = "Windows"
    elif "linux" in ua:
        os_name = "Linux"
    else:
        os_name = "?"
    return f"{browser} · {os_name}"
