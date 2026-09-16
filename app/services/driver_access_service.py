"""
Доступ водителя в приложение: выдача, вход, телефоны, отзыв.

Сценарий:
  1) Владелец в кабинете жмёт «Выдать доступ» — появляется ссылка и код.
  2) Водитель открывает ссылку в приложении или вводит код.
  3) Сервер гасит выдачу и заводит сессию на этот телефон.
  4) Владелец видит телефоны водителя и может отозвать любой или все сразу.

Паролей нет. В базе — только SHA-256 от ссылки, кода и токена сессии.

Решения, которые здесь зашиты (16.09.2026, ответы семи нейросетей и владельца —
`DRIVER_ACCESS_AI_ANSWERS.md`):
- выдача живёт 30 минут и гасится один раз;
- код — 8 символов без похожих знаков (0/O, 1/I), вводится с дефисом или без;
- у водителя до трёх телефонов («может быть 2–3 телефона»); один телефон на
  двоих не бывает — сессия принадлежит одному водителю;
- сессия водителя живёт в отдельной таблице и под отдельной cookie: открыть
  ею кабинет владельца невозможно.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Driver, DriverAccessGrant, DriverSession

GRANT_TTL = timedelta(minutes=30)
MAX_ACTIVE_DEVICES = 3
DRIVER_COOKIE = "driver_session"

# Без 0/O и 1/I/L: код диктуют голосом и переписывают с экрана.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _utc(moment: datetime | None) -> datetime | None:
    # Postgres отдаёт время с поясом, SQLite в тестах — без.
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def normalize_code(raw: str) -> str:
    """«abcd-efgh », «ABCD EFGH» → «ABCDEFGH». Похожие знаки не угадываем."""
    return "".join(ch for ch in (raw or "").upper() if ch.isalnum())


def format_code(code: str) -> str:
    """ABCDEFGH → ABCD-EFGH: так легче продиктовать."""
    return f"{code[:4]}-{code[4:]}"


@dataclass
class IssuedGrant:
    grant: DriverAccessGrant
    token: str   # показывается ОДИН раз — в базе только отпечаток
    code: str


async def issue_grant(
    session: AsyncSession,
    *,
    driver: Driver,
    issued_by_telegram_id: int | None,
    now: datetime | None = None,
) -> IssuedGrant:
    """Новая выдача. Прежние непогашенные выдачи этого водителя сгорают.

    ⚠️ Одна живая выдача на водителя: владелец мог нажать дважды, и первая
    ссылка, забытая в переписке, не должна оставаться рабочей.
    """
    now = now or datetime.now(timezone.utc)
    await session.execute(
        update(DriverAccessGrant)
        .where(
            DriverAccessGrant.driver_id == driver.id,
            DriverAccessGrant.used_at.is_(None),
            DriverAccessGrant.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    token = secrets.token_urlsafe(32)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    grant = DriverAccessGrant(
        owner_id=driver.owner_id,
        driver_id=driver.id,
        token_hash=_hash(token),
        code_hash=_hash(code),
        issued_by_telegram_id=issued_by_telegram_id,
        created_at=now,
        expires_at=now + GRANT_TTL,
    )
    session.add(grant)
    await session.flush()
    return IssuedGrant(grant=grant, token=token, code=code)


class RedeemError(Exception):
    """Понятная причина отказа — её видит водитель."""


async def _find_grant(
    session: AsyncSession, *, token: str | None, code: str | None
) -> DriverAccessGrant | None:
    if token:
        where = DriverAccessGrant.token_hash == _hash(token.strip())
    elif code:
        normalized = normalize_code(code)
        if len(normalized) != CODE_LENGTH:
            return None
        where = DriverAccessGrant.code_hash == _hash(normalized)
    else:
        return None
    return (
        await session.execute(select(DriverAccessGrant).where(where))
    ).scalar_one_or_none()


async def active_devices(session: AsyncSession, driver_id: int) -> list[DriverSession]:
    return list((
        await session.execute(
            select(DriverSession)
            .where(
                DriverSession.driver_id == driver_id,
                DriverSession.revoked_at.is_(None),
            )
            .order_by(DriverSession.created_at)
        )
    ).scalars().all())


@dataclass
class Redeemed:
    driver: Driver
    driver_session: DriverSession
    token: str   # уходит в cookie, в базе только отпечаток


async def redeem(
    session: AsyncSession,
    *,
    token: str | None,
    code: str | None,
    device_id: str,
    device_label: str | None,
    platform: str | None,
    app_version: str | None,
    ip: str | None,
    now: datetime | None = None,
) -> Redeemed:
    """Погасить выдачу и завести сессию на телефон.

    Отказы — одним понятным текстом, без подробностей, по которым можно
    подбирать: «нет такого кода» и «код чужого парка» выглядят одинаково.
    """
    now = now or datetime.now(timezone.utc)
    device_id = (device_id or "").strip()[:64]
    if not device_id:
        raise RedeemError("Приложение не прислало номер устройства. Обновите приложение.")

    grant = await _find_grant(session, token=token, code=code)
    if grant is None or grant.revoked_at is not None:
        raise RedeemError("Ссылка или код не подходят. Попросите владельца выдать новый доступ.")
    if grant.used_at is not None:
        raise RedeemError("Этот доступ уже использован. Попросите владельца выдать новый.")
    if _utc(grant.expires_at) < now:
        raise RedeemError("Срок доступа истёк (30 минут). Попросите владельца выдать новый.")

    driver = await session.get(Driver, grant.driver_id)
    if driver is None or not driver.is_active or driver.owner_id != grant.owner_id:
        raise RedeemError("Доступ отключён. Обратитесь к владельцу.")

    devices = await active_devices(session, driver.id)
    # Тот же телефон входит повторно (переустановили приложение и т.п.) —
    # старая сессия этого телефона гаснет, новая встаёт на её место.
    same_phone = [d for d in devices if d.device_id == device_id]
    for old in same_phone:
        old.revoked_at = now
    others = [d for d in devices if d.device_id != device_id]
    if len(others) >= MAX_ACTIVE_DEVICES:
        raise RedeemError(
            f"У водителя уже {len(others)} телефона. Попросите владельца "
            "отключить один из них в кабинете."
        )

    raw = secrets.token_urlsafe(48)
    ds = DriverSession(
        owner_id=driver.owner_id,
        driver_id=driver.id,
        grant_id=grant.id,
        token_hash=_hash(raw),
        device_id=device_id,
        device_label=(device_label or "")[:120] or None,
        platform=(platform or "")[:20] or None,
        app_version=(app_version or "")[:20] or None,
        ip=(ip or "")[:45] or None,
        created_at=now,
        last_seen_at=now,
    )
    session.add(ds)
    grant.used_at = now
    await session.flush()
    return Redeemed(driver=driver, driver_session=ds, token=raw)


async def session_by_token(
    session: AsyncSession, raw: str | None, *, now: datetime | None = None
) -> DriverSession | None:
    """Живая сессия водителя по cookie. last_seen обновляется раз в 5 минут."""
    if not raw:
        return None
    ds = (
        await session.execute(
            select(DriverSession).where(
                DriverSession.token_hash == _hash(raw),
                DriverSession.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if ds is None:
        return None
    now = now or datetime.now(timezone.utc)
    last = _utc(ds.last_seen_at)
    if last is None or (now - last).total_seconds() > 300:
        ds.last_seen_at = now
    return ds


async def revoke_device(
    session: AsyncSession, *, owner_id: int, session_id: int,
    now: datetime | None = None,
) -> DriverSession | None:
    """Отключить один телефон. Чужой парк — как будто такого нет."""
    ds = await session.get(DriverSession, session_id)
    if ds is None or ds.owner_id != owner_id:
        return None
    if ds.revoked_at is None:
        ds.revoked_at = now or datetime.now(timezone.utc)
    return ds


async def revoke_all(
    session: AsyncSession, *, driver: Driver, now: datetime | None = None
) -> int:
    """Отключить все телефоны водителя и сжечь непогашенные выдачи.

    ⚠️ Открытую смену НЕ закрываем: это создало бы факт, которого не было.
    Отправленные фото и действия остаются — это документы компании.
    """
    now = now or datetime.now(timezone.utc)
    result = await session.execute(
        update(DriverSession)
        .where(DriverSession.driver_id == driver.id, DriverSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.execute(
        update(DriverAccessGrant)
        .where(
            DriverAccessGrant.driver_id == driver.id,
            DriverAccessGrant.used_at.is_(None),
            DriverAccessGrant.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    return result.rowcount or 0


async def pending_grant(
    session: AsyncSession, driver_id: int, *, now: datetime | None = None
) -> DriverAccessGrant | None:
    """Выданный, но ещё не погашенный доступ — чтобы владелец видел «ждёт входа»."""
    now = now or datetime.now(timezone.utc)
    grant = (
        await session.execute(
            select(DriverAccessGrant)
            .where(
                DriverAccessGrant.driver_id == driver_id,
                DriverAccessGrant.used_at.is_(None),
                DriverAccessGrant.revoked_at.is_(None),
            )
            .order_by(DriverAccessGrant.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if grant is None or _utc(grant.expires_at) < now:
        return None
    return grant


async def device_counts(session: AsyncSession, owner_id: int) -> dict[int, int]:
    """Сколько живых телефонов у каждого водителя — для карточек в кабинете."""
    rows = await session.execute(
        select(DriverSession.driver_id, func.count())
        .where(DriverSession.owner_id == owner_id, DriverSession.revoked_at.is_(None))
        .group_by(DriverSession.driver_id)
    )
    return {driver_id: count for driver_id, count in rows.all()}
