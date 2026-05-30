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


def create_jwt(owner_id: int) -> str:
    payload = {
        "sub": str(owner_id),
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + JWT_TTL,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGO)


def decode_jwt(token: str) -> int | None:
    """Вернуть owner_id или None при ошибке/истечении."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGO])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        logger.debug("JWT decode failed: %s", exc)
        return None
