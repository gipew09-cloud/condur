"""
Фото из приложения водителя: приём, хранение, выдача владельцу.

Ссылка на фото в полях смены/рейса/расхода — `app-<id>`. Фото из Telegram
там же записаны своим file_id; по приставке кабинет понимает, откуда брать.

Что проверяется при приёме (замечания аудита 17.09.2026):
- размер — по мере чтения, а не после: огромный запрос не съест память;
- тип — по первым байтам файла, а не по тому, что назвал телефон;
- повтор того же фото (тот же `client_id`) не ложится второй раз.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Driver, DriverPhoto

PREFIX = "app-"

# Фото с камеры телефона в полном качестве весит 2–6 МБ; 15 — с запасом.
MAX_BYTES = 15 * 1024 * 1024

KINDS = {"odometer_start", "odometer_end", "waybill", "receipt"}

# Откуда фото. Чего-то другого телефон не присылает — неизвестное не пишем.
SOURCES = {"camera", "gallery"}

_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class PhotoRejected(Exception):
    """Фото не принято — текст для водителя."""


def sniff_type(head: bytes) -> str | None:
    """Тип картинки по первым байтам. Только то, что покажет любой браузер.

    ⚠️ HEIC (родной формат iPhone) сюда не входит: Chrome его не показывает.
    Приложение присылает JPEG.
    """
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def ref_of(photo: DriverPhoto) -> str:
    return f"{PREFIX}{photo.id}"


def parse_ref(ref: str | None) -> int | None:
    if not ref or not isinstance(ref, str) or not ref.startswith(PREFIX):
        return None
    tail = ref[len(PREFIX):]
    return int(tail) if tail.isdigit() else None


def is_app_ref(ref: str | None) -> bool:
    return parse_ref(ref) is not None


@dataclass
class Saved:
    photo: DriverPhoto
    duplicate: bool

    @property
    def ref(self) -> str:
        return ref_of(self.photo)


async def _existing(session: AsyncSession, driver_id: int, client_id: str) -> DriverPhoto | None:
    return (await session.execute(
        select(DriverPhoto).where(
            DriverPhoto.driver_id == driver_id, DriverPhoto.client_id == client_id,
        )
    )).scalar_one_or_none()


async def save(
    session: AsyncSession,
    *,
    driver: Driver,
    client_id: str,
    kind: str,
    data: bytes,
    taken_at: datetime | None = None,
    source: str | None = None,
) -> Saved:
    """Сохранить фото (коммит — на вызывающем). Повтор — прежнее фото."""
    client_id = (client_id or "").strip()
    if not _CLIENT_ID_RE.match(client_id):
        raise PhotoRejected("Приложение не прислало номер фото. Обновите приложение.")
    if kind not in KINDS:
        raise PhotoRejected("Неизвестный вид фото. Обновите приложение.")
    existing = await _existing(session, driver.id, client_id)
    if existing is not None:
        return Saved(existing, duplicate=True)
    if not data:
        raise PhotoRejected("Фото пустое — снимите ещё раз.")
    if len(data) > MAX_BYTES:
        raise PhotoRejected("Фото слишком большое.")
    content_type = sniff_type(data[:16])
    if content_type is None:
        raise PhotoRejected("Это не похоже на фото. Снимите ещё раз.")
    photo = DriverPhoto(
        owner_id=driver.owner_id,
        driver_id=driver.id,
        client_id=client_id,
        kind=kind,
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
        taken_at=taken_at,
        source=source if source in SOURCES else None,
    )
    try:
        async with session.begin_nested():
            session.add(photo)
    except IntegrityError:
        # То же фото пришло двумя запросами разом — берём записанное первым.
        existing = await _existing(session, driver.id, client_id)
        if existing is None:
            raise
        return Saved(existing, duplicate=True)
    return Saved(photo, duplicate=False)


async def for_driver(session: AsyncSession, driver_id: int, ref: str | None) -> DriverPhoto | None:
    """Фото этого водителя по ссылке — чужое не отдаём."""
    photo_id = parse_ref(ref)
    if photo_id is None:
        return None
    photo = await session.get(DriverPhoto, photo_id)
    if photo is None or photo.driver_id != driver_id:
        return None
    return photo


async def for_owner(session: AsyncSession, owner_id: int, ref: str | None) -> DriverPhoto | None:
    photo_id = parse_ref(ref)
    if photo_id is None:
        return None
    photo = await session.get(DriverPhoto, photo_id)
    if photo is None or photo.owner_id != owner_id:
        return None
    return photo


def origin_line(source: str | None) -> str | None:
    """Строка для владельца: откуда фото. Галерея — с предупреждением: такое
    фото могло быть снято когда угодно (например, вчерашний одометр)."""
    if source == "camera":
        return "📷 Снято камерой"
    if source == "gallery":
        return "🖼 Из галереи — могло быть снято раньше"
    return None


async def origins(session: AsyncSession, owner_id: int, refs) -> dict[str, str]:
    """Откуда фото — для нескольких ссылок разом (страницы кабинета, журнал).
    Фото из Telegram здесь нет: откуда оно, Telegram не сообщает."""
    ids = {parse_ref(ref): ref for ref in refs if parse_ref(ref) is not None}
    if not ids:
        return {}
    rows = (await session.execute(
        select(DriverPhoto.id, DriverPhoto.source).where(
            DriverPhoto.id.in_(list(ids)), DriverPhoto.owner_id == owner_id,
        )
    )).all()
    return {ids[pid]: source for pid, source in rows if source in SOURCES}


async def read_limited(stream, limit: int | None = None) -> bytes:
    """Прочитать загрузку кусками и бросить, как только перевалило за предел —
    не дожидаясь конца (замечание аудита: раньше читалось целиком)."""
    # ⚠️ Предел берём в момент вызова, а не при загрузке модуля.
    limit = MAX_BYTES if limit is None else limit
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(256 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise PhotoRejected("Фото слишком большое.")
        chunks.append(chunk)
    return b"".join(chunks)
