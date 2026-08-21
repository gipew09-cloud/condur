"""Мониторинг: трек, напряжение бортсети, обесточка, экран карты.

Проверяем то, что легко сломать молча: разрыв связи не должен превращаться
в прямую линию, значения-заглушки трекера не должны попадать в интерфейс,
а обесточенная машина обязана отличаться от просто заглушённой.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services import telemetry_service


# ------------------------------------------------------------------ напряжение
def test_sensor_voltage_rejects_tracker_sentinels():
    """65535 / -128 / -327.68 — это «датчика нет», а не показание."""
    assert telemetry_service.sensor_voltage(27.98) == Decimal("27.98")
    assert telemetry_service.sensor_voltage(4.23) == Decimal("4.23")
    for sentinel in (65535, -128, -327.68, 0, None, "мусор"):
        assert telemetry_service.sensor_voltage(sentinel) is None, sentinel


def test_power_cut_only_when_board_voltage_collapsed():
    """Масса выключена — борт почти в нуле, а точки продолжают идти."""
    # обычная работа и стоянка с заглушённым двигателем — не обесточка
    assert telemetry_service.power_cut(27.98, 4.23) is False
    assert telemetry_service.power_cut(25.30, 4.22) is False
    assert telemetry_service.power_cut(12.60, 4.10) is False
    # массу выключили
    assert telemetry_service.power_cut(0.4, 3.91) is True
    # напряжения нет вовсе — молчим, а не пугаем владельца
    assert telemetry_service.power_cut(None, None) is False
    assert telemetry_service.power_cut(65535, None) is False


# ------------------------------------------------------------------ трек
def _track_points():
    t0 = datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc)
    pts = []
    for i in range(40):                      # 20 минут едет
        pts.append((t0 + timedelta(seconds=30 * i), 59.90 + i * 0.001, 30.30, 55))
    stop_at = t0 + timedelta(minutes=20)
    for i in range(50):                      # 25 минут стоит
        pts.append((stop_at + timedelta(seconds=30 * i), 59.939, 30.30, 0))
    gone = stop_at + timedelta(minutes=25 + 40)   # 40 минут тишины
    for i in range(30):                      # и снова едет, уже в другом месте
        pts.append((gone + timedelta(seconds=30 * i), 59.98 + i * 0.001, 30.45, 48))
    return pts, gone + timedelta(minutes=15)


def test_track_gap_is_separate_segment_not_a_straight_line():
    """Разрыв связи — отдельный отрезок (на карте пунктир).

    Если склеить его с поездкой, получится «полёт через водохранилище»:
    линия утверждает маршрут, которого система не знает.
    """
    pts, window_end = _track_points()
    segs = telemetry_service.build_track_segments(pts, window_end=window_end)
    kinds = [s["kind"] for s in segs]
    assert "nosignal" in kinds, "разрыв связи потерялся"
    assert kinds.count("move") == 2, "поездки до и после разрыва должны быть разными"

    gap = next(s for s in segs if s["kind"] == "nosignal")
    # у разрыва ровно две точки — конец известного пути и начало следующего
    assert len(gap["points"]) == 2
    assert gap["points"][0] != gap["points"][1]

    # точки разрыва НЕ попали внутрь поездок
    for seg in (s for s in segs if s["kind"] == "move"):
        assert gap["points"][1] not in seg["points"] or gap["points"][0] not in seg["points"]


def test_track_move_segments_carry_geometry_and_distance():
    pts, window_end = _track_points()
    segs = telemetry_service.build_track_segments(pts, window_end=window_end)
    moves = [s for s in segs if s["kind"] == "move"]
    assert moves, "поездок нет"
    for seg in moves:
        assert len(seg["points"]) >= 2
        assert seg["distance_km"] > 0
        assert all(len(p) == 2 for p in seg["points"])
    # 40 точек по 0.001° широты ≈ 4.3 км
    assert 3.5 < moves[0]["distance_km"] < 5.5


def test_track_stop_has_place_and_duration():
    pts, window_end = _track_points()
    segs = telemetry_service.build_track_segments(pts, window_end=window_end)
    stops = [s for s in segs if s["kind"] == "stop"]
    assert stops, "стоянка не найдена"
    stop = stops[0]
    assert stop["lat"] and stop["lon"]
    assert stop["seconds"] > 0
    assert "мин" in stop["duration_label"] or "ч" in stop["duration_label"]


def test_track_empty_input_is_empty_track():
    assert telemetry_service.build_track_segments(
        [], window_end=datetime.now(timezone.utc)
    ) == []


# ------------------------------------------------------------------ экран
def test_vehicle_color_palette_matches_forms():
    """Палитра одна на всех: модели, роутер, обе формы машины."""
    from app.models import VEHICLE_COLORS

    assert list(VEHICLE_COLORS) == [
        "black", "white", "yellow", "orange", "red", "blue", "green"
    ]
    router = open("app/web/router.py", encoding="utf-8").read()
    assert "from app.models import" in router and "VEHICLE_COLORS" in router
    # цвет принимается обеими формами и нормализуется (мусор → чёрный)
    assert router.count('color: Annotated[str, Form()] = "black"') == 2
    assert "target.color = _vehicle_color(color)" in router
    assert "vehicle.color = _vehicle_color(color)" in router
    for tpl in ("app/web/templates/vehicles.html", "app/web/templates/_vehicle_row.html"):
        src = open(tpl, encoding="utf-8").read()
        assert 'name="color"' in src, tpl
        assert "VEHICLE_COLORS.items()" in src, tpl


def test_unknown_color_falls_back_to_black():
    import os

    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x/y")
    from app.web.router import _vehicle_color

    assert _vehicle_color("blue") == "blue"
    assert _vehicle_color(None) == "black"
    assert _vehicle_color("") == "black"
    assert _vehicle_color("<script>") == "black"


def test_monitoring_screen_draws_truck_picture_and_address():
    """Рисунок машины в метке, перекраска в canvas, адрес вместо координат."""
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    # рисунок один, цвета генерируются перекраской — отдельных файлов нет
    assert "/static/truck-reefer.png" in src
    assert "getImageData" in src and "toDataURL" in src
    # красим ТОЛЬКО кузов: резина, рама и стёкла темнее порога и не трогаются,
    # иначе выходит одноцветная клякса с синими колёсами
    assert "TRUCK_BODY_MIN_LUM" in src
    assert "if (lum < TRUCK_BODY_MIN_LUM) continue;" in src
    assert ".mon-truck--" in src
    # картинка стоит и в метке, и в строке списка, и в карточке
    assert "mon-pin__img mon-truck--" in src
    assert "mon-tile__img mon-truck--" in src
    # рисунок выбирается по типу кузова: пришлют тентованный и тягач —
    # добавляются строкой в TRUCK_ART, перекраска подхватит их сама
    assert "TRUCK_ART = {" in src
    for body in ("refrigerator", "truck", "gazelle"):
        assert body in src, body
    # ключ рисунка задан явно, а не выводится из имени файла
    assert "key: 'reefer'" in src
    assert "artFor(v.type).key" in src
    # обводка состояния осталась носителем состояния
    assert "'0 0 0 2.5px ' + st.color" in src
    # адрес берётся у геокодера Яндекса и кэшируется по округлённой точке
    assert "ymaps.geocode(" in src
    assert "addrCache" in src


def test_monitoring_track_is_a_gradient_like_the_design():
    """Трек — градиент от бледного к насыщенному, как в макете редизайна."""
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    assert "TRACK_FAR" in src and "TRACK_NEAR" in src
    assert "[147, 197, 253]" in src   # #93c5fd — дальний конец
    assert "[29, 78, 216]" in src     # #1d4ed8 — у машины
    assert "trackColor(t)" in src


def test_truck_picture_is_shipped_and_transparent():
    """Файл рисунка на месте, с альфа-каналом (иначе в метке будет белый прямоугольник)."""
    png = open("app/web/static/truck-reefer.png", "rb").read()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR: ширина, высота, глубина, тип цвета (6 = RGBA)
    width = int.from_bytes(png[16:20], "big")
    color_type = png[25]
    assert width >= 256, width
    assert color_type == 6, f"нужен RGBA, а не тип {color_type}"


def test_truck_artwork_has_dark_parts_to_protect():
    """В рисунке должны быть и светлый кузов, и тёмные детали.

    Перекраска красит только пиксели ярче порога. Если однажды подложить
    рендер целиком тёмной машины, красить будет нечего — а если целиком
    светлой, покрасятся и колёса. Тест ловит обе подмены.
    """
    import struct
    import zlib

    raw = open("app/web/static/truck-reefer.png", "rb").read()
    # разбираем PNG вручную: Pillow в зависимостях проекта нет
    pos, idat, meta = 8, b"", None
    while pos < len(raw):
        (length,) = struct.unpack(">I", raw[pos:pos + 4])
        ctype = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            meta = struct.unpack(">IIBB", data[:10])
        elif ctype == b"IDAT":
            idat += data
        pos += 12 + length
    width, height, depth, color_type = meta
    assert (depth, color_type) == (8, 6), "нужен 8-битный RGBA"

    rows = zlib.decompress(idat)
    stride = width * 4
    prev = bytearray(stride)
    dark = light = opaque = 0
    at = 0
    for _ in range(height):
        filt = rows[at]
        line = bytearray(rows[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):                       # разворачиваем фильтры PNG
            a = line[i - 4] if i >= 4 else 0
            b = prev[i]
            c = prev[i - 4] if i >= 4 else 0
            if filt == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filt == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filt == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif filt == 4:
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        for x in range(0, stride, 4):
            if line[x + 3] < 128:
                continue
            opaque += 1
            lum = (line[x] * 299 + line[x + 1] * 587 + line[x + 2] * 114) / 255000
            if lum < 0.62:
                dark += 1
            else:
                light += 1
        prev = line

    assert opaque > 1000, "рисунок пустой"
    # колёса, рама, стёкла — заметная доля; кузов — тоже
    assert dark / opaque > 0.15, f"тёмных деталей всего {dark / opaque:.0%} — красить будет нечего"
    assert light / opaque > 0.30, f"светлого кузова всего {light / opaque:.0%}"


def test_monitoring_screen_has_track_camera_and_voltage():
    """Ключевые куски экрана мониторинга на месте."""
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    # трек грузится с нашего эндпоинта
    assert "/api/vehicles/' + vehicleId + '/track?hours=" in src
    # разрыв связи рисуется пунктиром, а не сплошной
    assert "strokeStyle: 'dash'" in src
    assert "#c5362b" in src
    # камера едет за выбранной машиной и отпускает карту, если её тронули
    assert "setFollow(selected)" in src
    assert "mousedown" in src and "setFollow(null)" in src
    # напряжение бортсети в карточке
    assert "Напряжение" in src
    # метка уменьшается на общем плане
    assert "pinSizeForZoom" in src


def test_locations_api_exposes_voltage_and_zone():
    src = open("app/web/router.py", encoding="utf-8").read()
    assert '"voltage": float(st.voltage)' in src
    assert '"power_cut": telemetry_service.power_cut(' in src
    assert 'v["zone"] = zone.name' in src
    # эндпоинт трека существует и ограничен владельцем
    assert '@app.get("/api/vehicles/{vehicle_id}/track")' in src
    assert "vehicle.owner_id != owner.id" in src


def test_receiver_stores_voltage_from_both_protocols():
    src = open("app/telemetry/egts_receiver.py", encoding="utf-8").read()
    # Wialon (ретрансляция Ставтрэка) — params['power'] / ['battery']
    assert 'wp.params.get("power")' in src and 'wp.params.get("battery")' in src
    # EGTS (своё железо) — из подзаписи состояния терминала
    assert "rec.state.main_power_v" in src and "rec.state.backup_battery_v" in src
    # и то, и другое попадает в «быстрый слой» для карты
    assert "voltage=last_good.voltage" in src
