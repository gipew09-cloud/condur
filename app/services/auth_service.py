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

from app.config import settings

logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=5)


# Сколько неверных попыток допускаем на один код. Без этого 6-значный код
# (миллион вариантов) можно было подобрать перебором: Telegram ID не секрет,
# а неверная попытка раньше ничего не стоила. После лимита код сгорает —
# нужно заново просить /login у бота.
MAX_CODE_ATTEMPTS = 5


@dataclass
class CodeEntry:
    code: str
    expires_at: datetime
    attempts_left: int = MAX_CODE_ATTEMPTS


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
    """Проверить код. На успехе удалить, чтобы был одноразовым.

    Неверная попытка расходует лимит; когда попытки кончились — код сгорает.
    Сравнение постоянного времени: чтобы по скорости ответа нельзя было
    подбирать код посимвольно.
    """
    entry = _login_codes.get(telegram_id)
    if entry is None:
        return False
    if entry.expires_at < datetime.now(timezone.utc):
        _login_codes.pop(telegram_id, None)
        return False
    if not secrets.compare_digest(entry.code, code.strip()):
        entry.attempts_left -= 1
        if entry.attempts_left <= 0:
            _login_codes.pop(telegram_id, None)
        return False
    _login_codes.pop(telegram_id, None)
    return True


# JWT-вход убран (аудит безопасности): он подписывался JWT_SECRET, а дефолт
# секрета «change-me-in-production» лежал в .env.example на GitHub. Кто знал
# секрет — мог подделать cookie и войти под любым владельцем. Теперь вход
# только по постоянной сессии (web_sessions) ниже.


# =====================================================================
# Постоянные сессии (таблица web_sessions): вход живёт, пока его не
# завершат. В cookie — случайный токен, в БД — только SHA-256 от него.
# =====================================================================
SESSION_COOKIE = "session"
SESSION_COOKIE_MAX_AGE = 10 * 365 * 24 * 3600  # «навсегда» (10 лет)


def set_session_cookie(response, raw_token: str) -> None:
    """Поставить cookie сессии так, чтобы она НЕ слетала — важно для iPhone/Safari.

    Ключевое для Safari на iOS: (1) Secure — Safari строг к cookie без него на
    HTTPS и может их сбрасывать; (2) явный Expires рядом с Max-Age — часть версий
    iOS смотрит на Expires, иначе считает cookie сессионной и удаляет при
    сворачивании вкладки; (3) Path=/ и SameSite=Lax — вход по редиректу после
    POST /login должен доносить cookie.
    """
    from datetime import datetime, timedelta, timezone

    expires = datetime.now(timezone.utc) + timedelta(seconds=SESSION_COOKIE_MAX_AGE)
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        max_age=SESSION_COOKIE_MAX_AGE,
        expires=expires,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def clear_session_cookie(response) -> None:
    """Удалить cookie сессии теми же атрибутами (иначе браузер её не сотрёт)."""
    response.delete_cookie(
        SESSION_COOKIE, path="/", secure=settings.cookie_secure, samesite="lax"
    )


def new_session_token() -> str:
    return secrets.token_urlsafe(48)


def session_token_hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


def device_label_from_user_agent(user_agent: str | None) -> str:
    """Короткая подпись устройства для списка «Устройства»: «Chrome · Windows»."""
    ua = (user_agent or "").lower()
    # ⚠️ Приложение Condur ставится файлом на телефон и браузером не является.
    # Свой признак ему нужен потому, что оно шлёт «Condur App (android)»: ни
    # одного знакомого браузера в строке нет, и без этой ветки телефон попадал
    # в список как «Браузер · Android» — не отличить от кабинета, открытого в
    # Chrome на том же телефоне. Проверка идёт первой: строка приложения не
    # должна случайно совпасть с браузерной веткой.
    if "condur app" in ua:
        # Приложение подставляет сюда своё название системы: «android», «iOS».
        # У «ios» нет знакомого браузерам следа, поэтому разбираем отдельно —
        # общую проверку трогать нельзя, там «ios» встретится в чужих строках.
        system = _os_from_user_agent(ua)
        if system == "?" and "ios" in ua:
            system = "iPhone"
        return f"Приложение Condur · {system}"
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
    return f"{browser} · {_os_from_user_agent(ua)}"


def _os_from_user_agent(ua: str) -> str:
    """Система устройства по уже приведённой к нижнему регистру строке."""
    if "iphone" in ua:
        return "iPhone"
    if "ipad" in ua:
        return "iPad"
    if "android" in ua:
        return "Android"
    if "mac os" in ua or "macintosh" in ua:
        return "macOS"
    if "windows" in ua:
        return "Windows"
    if "linux" in ua:
        return "Linux"
    return "?"
