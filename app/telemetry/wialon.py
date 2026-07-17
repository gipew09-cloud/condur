"""
Парсер протокола Wialon IPS (текстовый) — второй протокол ретрансляции
Stavtrack, в котором, в отличие от их EGTS, передаются ДАТЧИКИ (зажигание,
напряжение и т.п.).

Формат подсмотрен у живой ретрансляции 16.07.2026 (наш приёмник записал
`#L#128507;NA` — логин Wialon IPS 1.1) и дополнен по открытой спецификации:

  #L#<terminal>;<password>            → ответ #AL#1
  #L#2.0;<terminal>;<password>;<crc>  → то же, версия 2.0
  #P#                                 → пинг, ответ #AP#
  #SD#date;time;lat1;lat2;lon1;lon2;speed;course;height;sats
                                      → короткая точка, ответ #ASD#1
  #D#...как SD...;hdop;inputs;outputs;adc;ibutton;params
                                      → полная точка (params с датчиками),
                                        ответ #AD#1
  #B#msg1|msg2|...                    → пачка SD/D-тел, ответ #AB#<n>

Координаты — «градусоминуты»: 5951.6834;N = 59° 51.6834' N.
Время — UTC, DDMMYY;HHMMSS. Пустые значения — «NA».
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Ключи параметров, в которых разные конфигурации трекеров передают зажигание.
IGNITION_PARAM_KEYS = ("ign", "ignition", "acc", "din1", "in1")


@dataclass(frozen=True)
class WialonPoint:
    observed_at: datetime | None
    latitude: float | None
    longitude: float | None
    speed_kmh: float
    course: float | None
    ignition: bool | None
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return (
            self.observed_at is not None
            and self.latitude is not None
            and self.longitude is not None
        )


@dataclass(frozen=True)
class WialonMessage:
    kind: str                 # "L" | "P" | "D" | "SD" | "B" | "?"
    terminal_id: str | None = None
    points: list[WialonPoint] = field(default_factory=list)


def _num(value: str) -> float | None:
    value = value.strip()
    if not value or value.upper() == "NA":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _coord(value: str, hemisphere: str) -> float | None:
    """5951.6834;N → 59.86139 (градусы + минуты/60, знак по полушарию)."""
    raw = _num(value)
    if raw is None:
        return None
    degrees = int(raw // 100)
    minutes = raw - degrees * 100
    decimal = degrees + minutes / 60
    if hemisphere.strip().upper() in ("S", "W"):
        decimal = -decimal
    return decimal


def _dt(date_s: str, time_s: str) -> datetime | None:
    """DDMMYY;HHMMSS (UTC) → datetime. NA/мусор → None."""
    date_s, time_s = date_s.strip(), time_s.strip()
    if len(date_s) != 6 or len(time_s) != 6 or not (date_s + time_s).isdigit():
        return None
    try:
        return datetime(
            2000 + int(date_s[4:6]), int(date_s[2:4]), int(date_s[0:2]),
            int(time_s[0:2]), int(time_s[2:4]), int(time_s[4:6]),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def parse_params(raw: str) -> dict[str, Any]:
    """'ign:1:1,pwr_ext:2:27.9' → {'ign': 1, 'pwr_ext': 27.9} (тип 3 — строка)."""
    result: dict[str, Any] = {}
    for chunk in raw.split(","):
        parts = chunk.split(":", 2)
        if len(parts) != 3:
            continue
        name, type_code, value = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not name:
            continue
        try:
            if type_code == "1":
                result[name] = int(value)
            elif type_code == "2":
                result[name] = float(value)
            else:
                result[name] = value
        except ValueError:
            result[name] = value
    return result


def ignition_from(params: dict[str, Any], inputs: float | None) -> bool | None:
    """Зажигание: сначала именованные параметры, потом бит 0 в inputs."""
    for key in IGNITION_PARAM_KEYS:
        for name, value in params.items():
            if name.lower() == key:
                try:
                    return bool(int(float(value)))
                except (TypeError, ValueError):
                    continue
    if inputs is not None:
        return bool(int(inputs) & 1)
    return None


def _point_from_fields(fields: list[str]) -> WialonPoint | None:
    """Тело SD (10 полей) или D (16 полей) → точка. Меньше 10 полей — мусор."""
    if len(fields) < 10:
        return None
    observed_at = _dt(fields[0], fields[1])
    latitude = _coord(fields[2], fields[3])
    longitude = _coord(fields[4], fields[5])
    speed = _num(fields[6]) or 0.0
    course = _num(fields[7])
    params: dict[str, Any] = {}
    inputs: float | None = None
    if len(fields) >= 16:  # полный #D#
        inputs = _num(fields[11])
        params = parse_params(fields[15]) if fields[15] else {}
    return WialonPoint(
        observed_at=observed_at,
        latitude=latitude,
        longitude=longitude,
        speed_kmh=float(speed),
        course=course,
        ignition=ignition_from(params, inputs),
        params=params,
    )


def parse_message(line: str) -> WialonMessage:
    """Одна строка протокола (без \\r\\n) → структура. Неизвестное → kind='?'."""
    line = line.strip()
    if not line.startswith("#"):
        return WialonMessage(kind="?")
    try:
        _, kind, payload = line.split("#", 2)
    except ValueError:
        return WialonMessage(kind="?")
    kind = kind.upper()

    if kind == "L":
        parts = payload.split(";")
        # 1.1: terminal;password    2.0: 2.0;terminal;password;crc
        terminal = parts[1] if parts and parts[0] == "2.0" and len(parts) > 1 else parts[0]
        terminal = terminal.strip()
        return WialonMessage(kind="L", terminal_id=terminal or None)
    if kind == "P":
        return WialonMessage(kind="P")
    if kind in ("SD", "D"):
        point = _point_from_fields(payload.split(";"))
        return WialonMessage(kind=kind, points=[point] if point else [])
    if kind == "B":
        points = []
        for body in payload.split("|"):
            point = _point_from_fields(body.split(";"))
            if point is not None:
                points.append(point)
        return WialonMessage(kind="B", points=points)
    return WialonMessage(kind="?")


def ack_for(message: WialonMessage) -> bytes | None:
    """Квитанция по типу сообщения — без неё ретранслятор ретраит и рвёт связь."""
    if message.kind == "L":
        return b"#AL#1\r\n"
    if message.kind == "P":
        return b"#AP#\r\n"
    if message.kind == "SD":
        return b"#ASD#1\r\n"
    if message.kind == "D":
        return b"#AD#1\r\n"
    if message.kind == "B":
        return f"#AB#{len(message.points)}\r\n".encode()
    return None
