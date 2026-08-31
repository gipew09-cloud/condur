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


def test_track_does_not_draw_a_straight_line_through_city_blocks():
    """Пропала пачка точек посреди поездки — путь неизвестен, рисуем пунктир.

    Боевой случай 22.08.2026: трекер замолчал на несколько минут, машина за это
    время проехала по улицам несколько километров, а на карте появилась прямая
    линия наискосок через кварталы. Такая линия УТВЕРЖДАЕТ маршрут, которого
    система не знает, и завышает пробег.
    """
    t0 = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
    pts = []
    for i in range(20):                       # едет 10 минут
        pts.append((t0 + timedelta(seconds=30 * i), 59.800 + i * 0.0003, 30.400, 50))
    # тишина 6 минут — короче SEGMENT_GAP_SECONDS (15 мин), поэтому раньше
    # это НЕ считалось разрывом и точки соединялись прямой
    jump_at = t0 + timedelta(minutes=10 + 6)
    for i in range(20):                       # и продолжает ехать в 5 км оттуда
        pts.append((jump_at + timedelta(seconds=30 * i), 59.850 + i * 0.0003, 30.470, 50))

    segs = telemetry_service.build_track_segments(
        pts, window_end=jump_at + timedelta(minutes=10)
    )
    kinds = [s["kind"] for s in segs]
    assert kinds.count("move") == 2, "поездку не разрезали в месте пропажи точек"
    gaps = [s for s in segs if s["kind"] == "nosignal"]
    assert len(gaps) == 1, "пропуск пути не помечен"
    assert gaps[0]["reason"] == "no_points"
    assert len(gaps[0]["points"]) == 2

    # скачок не попал в пробег: 5 км «через дома» приписывать машине нельзя
    driven = sum(s["distance_km"] for s in segs if s["kind"] == "move")
    assert driven < 2.0, f"пробег завышен скачком: {driven} км"


def test_track_keeps_normal_highway_legs_solid():
    """На трассе точки далеко друг от друга — это НЕ повод рвать линию."""
    t0 = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
    # 40 секунд между точками, 90 км/ч → около километра за шаг
    pts = [
        (t0 + timedelta(seconds=40 * i), 59.800 + i * 0.009, 30.400, 90)
        for i in range(20)
    ]
    segs = telemetry_service.build_track_segments(
        pts, window_end=t0 + timedelta(minutes=20)
    )
    assert [s["kind"] for s in segs].count("nosignal") == 0, "трасса разрезана зря"
    assert [s["kind"] for s in segs].count("move") == 1


def test_track_teleport_is_a_gap_even_within_seconds():
    """Скачок GPS на километры за секунды — это не поездка, а сбой."""
    t0 = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
    pts = [
        (t0, 59.800, 30.400, 40),
        (t0 + timedelta(seconds=30), 59.801, 30.400, 40),
        # 20 км за 30 секунд — 2400 км/ч
        (t0 + timedelta(seconds=60), 59.980, 30.400, 40),
        (t0 + timedelta(seconds=90), 59.981, 30.400, 40),
    ]
    segs = telemetry_service.build_track_segments(
        pts, window_end=t0 + timedelta(minutes=5)
    )
    assert any(s["kind"] == "nosignal" for s in segs), "телепорт нарисован как поездка"


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
    # обводка состояния осталась носителем состояния (теперь через ringCss/applyRing)
    assert "applyRing(tile, key, stateColor(key), 2.5" in src
    # адрес берётся у своего эндпоинта (он кэширует и сам выбирает геокодер);
    # ymaps.geocode использовать нельзя — для него нужен ОТДЕЛЬНЫЙ ключ Яндекса,
    # и без него вызов молча падал, а карточка навсегда писала «определяю адрес…»
    assert "/api/geocode/reverse?lat=" in src
    assert "ymaps.geocode(" not in src
    assert "addrCache" in src
    # неудачу показываем честно, а не вечным «определяю…»
    assert "адрес не определился" in src


def test_monitoring_has_tail_and_direction_arrow_like_the_app():
    """Хвост и стрелка направления есть и в кабинете, не только в приложении.

    Владелец 27.08.2026: «причём тут приложение, когда я говорю — вообще в
    целом проект, надо делать везде, на сайте этого нет». Функция, сделанная
    в одной поверхности, для него не сделана.

    ⚠️ Правила продублированы в Dart (`lib/geo.dart`, `DirectionRing`) и здесь.
    Разъедутся — телефон и кабинет покажут про одну машину разное.
    """
    src = open("app/web/templates/map.html", encoding="utf-8").read()

    # хвост: только куски «ехал», окно 15 минут, потолок точек
    assert "TAIL_WINDOW_MS = 15 * 60 * 1000" in src
    assert "TAIL_MAX_POINTS = 30" in src
    assert "if (seg.kind !== 'move') return;" in src
    # Хвост ВСЕГДА синий и от цвета машины не зависит: это след, а не машина.
    # Оттенок — по теме: бледный на тёмной карте, насыщенный на светлой, где
    # бледно-синий теряется среди дорог того же тона.
    assert "function tailColor()" in src
    assert "#93c5fd" in src            # тёмная карта — как в приложении
    assert "#2f7bf6" in src            # светлая карта
    assert "0.18 + t * 0.82" in src    # яркости прибавлено по просьбе владельца

    # Хвост ТАЕТ к дальнему концу, а не обрывается: у машины плотный, там где
    # она была 15 минут назад — сходит на нет. Владелец 27.08: «пусть
    # живучесть будет 15 минут, и она постепенно уходит».
    assert "TAIL_CHUNKS = 12" in src   # столько же ступеней, сколько в приложении
    assert "2.2 + t * 2.8" in src      # и толщина растёт к машине

    # хвост появляется сам по выбору машины и обновляется вместе с опросом
    assert "syncTail()" in src
    # полный трек и хвост одновременно не рисуем — две линии по одному пути
    assert "clearTail();" in src

    # стрелка направления: порог как в приложении, курс считаем сами
    assert "HEADING_MIN_MOVE_M = 15" in src
    assert "Math.atan2(dLon, dLat)" in src
    # поправка на сходимость меридианов — иначе все направления косят к востоку
    assert "Math.cos(v.lat * Math.PI / 180)" in src
    # стрелка только у ЕДУЩЕЙ машины и только при известном направлении
    assert "key === 'moving' && course !== undefined" in src


def test_map_theme_follows_the_system_and_does_not_blank_out():
    """Тема сама идёт за системой, а карта не гаснет при переключении.

    Владелец 27.08.2026: «машины долго загружаются, когда меняется тема» и
    «почему-то автоматически не делается».
    """
    src = open("app/web/templates/map.html", encoding="utf-8").read()

    # следуем за системой, пока владелец не выбрал тему кнопкой
    assert "prefers-color-scheme: dark" in src
    assert "if (savedTheme) return;" in src   # ручной выбор сильнее системы

    # ⚠️ новый слой добавляем ДО удаления старого: иначе карта на секунду
    # пустая, и это читается как «машины пропали»
    add_at = src.index("ymap.addChild(schemeLayer);\n        if (previous)")
    assert add_at > 0

    # ⚠️ Подъём слоя меток при смене темы ОБЯЗАТЕЛЕН. Я убирал его ради
    # скорости, и владелец 30.08 поймал регресс: «когда выбираешь трек и
    # делаешь белую карту, карта белой не становится» — новая подложка
    # оставалась под старой. Правильность важнее паузы на перестроение.
    assert "YMapDefaultFeaturesLayer({ zIndex: 1800 })" in src
    theme_fn = src[src.index("function applyMapTheme()"):src.index("function raiseFeatures()")]
    code = [ln for ln in theme_fn.splitlines() if not ln.strip().startswith("//")]
    assert "raiseFeatures()" in "\n".join(code)


def test_tracks_tab_has_a_player_like_the_design():
    """Вкладка «Треки» с плеером — по макету «Редизайн системы мониторинга».

    Владелец 27.08.2026: «треки недоделаны», нужен плеер как в Ставтрэке —
    пауза, ускорение, ползунок и произвольный период «с… по…».
    """
    src = open("app/web/templates/map.html", encoding="utf-8").read()

    # вкладки панели: Объекты · Треки
    assert 'data-tab="objects"' in src and 'data-tab="tracks"' in src

    # период «с… по…» и быстрые чипы из макета
    assert 'id="mon-from"' in src and 'id="mon-to"' in src
    for quick in ("today", "yesterday", "week"):
        assert 'data-quick="%s"' % quick in src

    # смены за период тянем у своего эндпоинта
    assert "/api/vehicles/' + selected + '/shifts?from=" in src

    # плеер: пауза, скорости, полоса прогресса, часы по треку
    assert 'id="mon-play-toggle"' in src
    for speed in ("1", "2", "5", "15", "60"):
        assert 'data-speed="%s"' % speed in src
    # шаг по одной точке — для разбора спорной минуты
    assert 'id="mon-play-prev"' in src and 'id="mon-play-next"' in src
    assert "linear-gradient(90deg, #2563eb, #93c5fd)" in src

    # ⚠️ Ускорение считается по ВРЕМЕНИ трека, а не по числу точек: иначе
    # стоянка пролетала бы так же быстро, как езда.
    assert "realMs * play.speed * PLAY_BASE" in src
    # ⚠️ База 10, а не 60: при 60 даже ×1 гнал час трека за минуту
    # ⚠️ База = 1: «×1» на кнопке означает реальное время, а не «×4».
    # Владелец говорил «быстро» трижды именно потому, что подпись врала.
    assert "var PLAY_BASE = 1;" in src
    assert "×1 — как в жизни" in src
    # и не больше ОДНОЙ точки за кадр — иначе машина прыгает

    # ⚠️ Время каждой точки приходит с сервера (seg.times). Раскладывать точки
    # равномерно внутри отрезка нельзя — часы плеера будут врать.
    assert "times[i] ? new Date(times[i]).getTime() : NaN" in src

    # ⚠️ Объекты карты создаются ОДИН раз и дальше обновляются. Пересоздание в
    # цикле анимации давало то самое «всё моргает и лагает» (владелец 30.08).
    assert "play.line.update({ geometry:" in src
    assert "play.marker.update({ coordinates:" in src
    assert "if (play.at === play.drawn) return;" in src
    seek = src[src.index("function seekTo(index)"):src.index("async function refresh()")]
    assert "new YMapFeature" not in seek and "new YMapMarker" not in seek

    # Карточка качества периода: одометр и GPS рядом, «нет данных» вслух.
    assert 'id="mon-quality"' in src
    assert "Данных нет за период" in src
    assert "Показания уменьшились" in src        # сброс одометра не склеиваем
    assert "Показано не всё" in src              # упёрлись в лимит точек
    # ⚠️ имена полей называют источник километров
    assert "data.gps_path_distance_km" in src
    assert "sh.odometer_distance_km" in src
    assert "data.distance_km" not in src

    # ⚠️ Прокрутка ОДНА на всю вкладку «Треки». Отдельные скроллы у карточки и
    # списка на низком окне выпихивали список за пределы панели — измерено в
    # браузере, не на глаз.
    assert ".mon-tracks { display: flex; flex-direction: column; overflow-y: auto;" in src

    # поля периода обязаны сжиматься, иначе панель распирает и режет поиск
    assert "flex: 1; min-width: 0; display: flex; align-items: center; gap: 6px;" in src

    # Приборы на текущей точке: зажигание, топливо, температура.
    # ⚠️ Прибор промолчал — «нет данных», прошлое значение не подставляем.
    assert "Топливо <b>нет данных</b>" in src
    assert "Темп. топлива" in src
    assert "Зажигание" in src
    # предупреждение о разрыве связи прямо в плеере
    assert "до этой точки связи не было" in src
    # стрелка направления на кружке — по следующей точке, а не наугад
    assert "play.arrow.style.transform" in src
    # период больше суток — в часах появляется дата
    assert "play.multiday" in src

    # ⚠️ Просмотр ограничен ПЕРЕСЕЧЕНИЕМ смены и выбранного периода. Смена
    # могла тянуться с 26-го по 28-е (водитель забыл закрыть), а владелец
    # выбрал одни сутки — трек не имеет права уезжать за его выбор.
    assert "function clipToPeriod(row)" in src
    assert "var span = clipToPeriod(row);" in src

    # ⚠️ Значок ▶ рисуем сами: глиф из шрифта иконок в круглой кнопке всегда
    # съезжал вниз-вправо, владелец просил поправить это четыре раза.
    assert "var PLAY_SVG =" in src and 'class="ph ph-play"' not in src

        # События водителя на треке: место считается по времени, а если точек в эту
    # минуту нет — метки НЕТ, событие уходит в список «без места».
    assert "var EVENT_MATCH_MS = 5 * 60 * 1000;" in src
    assert "play.eventsNoPlace.push(ev);" in src
    assert "в эти минуты GPS молчал" in src
    assert "/events?from=" in src

    # камера едет за машиной по треку
    assert "ymap.setLocation({ center: LL(frame.lat, frame.lon)" in src

    # ⚠️ У панелей свой display, он специфичнее браузерного [hidden] — без
    # этого правила «Объекты» и «Треки» видны одновременно
    assert ".mon-panel--list [data-pane][hidden] { display: none; }" in src

    # тёмная версия есть: акценты вкладок и плеера переопределены
    assert '.mon[data-mode="dark"] .mon-tab.is-on' in src
    assert '.mon[data-mode="dark"] .mon-play__speeds button.is-on' in src


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


# ------------------------------------------------------------------ топливо
def test_fuel_level_read_from_real_stavtrack_packet():
    """Боевой пакет Т557ОС178 от 25.08.2026 — датчик на канале 2."""
    real = {"fuel2": 3608, "fuel3": 65535, "fuel4": 65535,
            "fuelTemp2": 18, "fuelTemp3": -128}
    assert telemetry_service.fuel_level_raw(real) == Decimal("3608")
    assert telemetry_service.fuel_temp_c(real) == Decimal("18")


def test_fuel_sentinels_are_not_readings():
    """65535 и -128 — «датчика нет», а не показание.

    Если пустить их в интерфейс, владелец увидит «65535» и перестанет верить
    экрану (та же ловушка, что с напряжением).
    """
    empty = {"fuel2": 65535, "fuel3": 65535, "fuelTemp2": -128}
    assert telemetry_service.fuel_level_raw(empty) is None
    assert telemetry_service.fuel_temp_c(empty) is None
    # мусор и пустота не должны падать
    for bad in (None, {}, {"fuel1": "абв"}, {"fuel1": -5}, {"fuel1": 999999}):
        assert telemetry_service.fuel_level_raw(bad) is None


def test_fuel_channel_is_not_hardcoded():
    """Канал ищем перебором: на другой машине ДУТ может стоять не на втором."""
    assert telemetry_service.fuel_level_raw({"fuel5": 1200}) == Decimal("1200")
    # беспроводной BLE-датчик (каналы 8…15 по документации УМКа302)
    assert telemetry_service.fuel_level_raw({"fuel8": 900}) == Decimal("900")
    # температура берётся с ТОГО ЖЕ канала, где нашёлся уровень
    assert telemetry_service.fuel_temp_c({"fuel5": 1200, "fuelTemp5": 12}) == Decimal("12")
    assert telemetry_service.fuel_temp_c({"fuel5": 1200, "fuelTemp2": 99}) is None


def test_fuel_is_stored_by_receiver_in_every_branch():
    """Топливо должно попадать и в историю, и в «быстрый слой».

    Отдельно проверяем ветку недостоверного GPS: уровень в баке от спутников
    не зависит, датчик меряет его и когда координаты врут.
    """
    src = open("app/telemetry/egts_receiver.py", encoding="utf-8").read()
    assert "fuel_level_raw=fuel_raw" in src, "не пишем в точку истории"
    # в списках обновляемых колонок — во всех четырёх ветках upsert
    assert src.count('"fuel_level_raw", "fuel_temp_c",') == 4
    assert "fuel_level_raw=last_any.fuel_level_raw" in src, "ветка плохого GPS без топлива"


def test_card_shows_litres_only_with_calibration():
    """Литры — только когда есть тарировка. Иначе честная надпись, не сырое число.

    Сырое значение датчика (3608) владельцу ничего не говорит, а выдать его за
    литры — соврать.
    """
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    assert "fuelMetrics" in src
    assert "нужна тарировка" in src            # тарировки нет
    assert "датчик не подключён" in src        # датчика нет
    assert "Math.round(v.fuel_litres)" in src  # тарировка есть → литры
    # сырое значение на экран не выводим ни при каком раскладе
    assert "v.fuel_raw.toFixed" not in src
    # процент бака — только если объём задан, иначе это выдумка
    assert "if (v.tank_litres)" in src


# ------------------------------------------------------------------ тарировка
def test_calibration_converts_real_stavtrack_table():
    """Пары со скриншота Ставтрэка (Датчики → Топливо → Дополнительные)."""
    cal = [[1, 0], [73, 10], [168, 20], [259, 30], [331, 40], [417, 50]]
    # узловые точки совпадают ровно
    assert telemetry_service.fuel_litres(73, cal) == Decimal("10.0")
    assert telemetry_service.fuel_litres(331, cal) == Decimal("40.0")
    # между узлами — по прямой, как считает и сам Ставтрэк своими a и b
    assert telemetry_service.fuel_litres(120, cal) == Decimal("14.9")
    # за краями таблицы НЕ экстраполируем: у дна и у горловины датчик врёт
    assert telemetry_service.fuel_litres(0, cal) == Decimal("0")
    assert telemetry_service.fuel_litres(99999, cal) == Decimal("50")
    # нет таблицы или нет показания — нет и литров
    assert telemetry_service.fuel_litres(3608, None) is None
    assert telemetry_service.fuel_litres(None, cal) is None


# Полная тарировка бака Т557ОС178, переписанная из Ставтрэка 25.08.2026
# (Автопарк → Т 557 ОС 178 СМАРТ → Датчики → Топливо → Дополнительные).
REAL_CALIBRATION = [
    [1, 0], [73, 10], [168, 20], [259, 30], [331, 40], [417, 50], [592, 75],
    [756, 100], [908, 125], [1080, 150], [1232, 175], [1380, 200], [1526, 225],
    [1667, 250], [1809, 275], [1949, 300], [2089, 325], [2229, 350], [2371, 375],
    [2511, 400], [2656, 425], [2799, 450], [2949, 475], [3105, 500], [3165, 510],
    [3229, 520], [3296, 530], [3365, 540], [3436, 550], [3505, 560], [3585, 570],
    [3630, 575], [3678, 580], [3692, 585],
]


def test_calibration_matches_stavtrack_own_coefficients():
    """Наш пересчёт обязан совпадать со Ставтрэком до сотых.

    Ставтрэк на каждый отрезок хранит прямую y = a·x + b и показывает её в той же
    таблице. Берём две его строки и сверяем: если разойдёмся, владелец увидит в
    двух системах разные литры по одной и той же машине и не поверит ни одной.
    """
    # X=73:  a=0.105263157, b=2.315789473  → ровно 10 л
    assert telemetry_service.fuel_litres(73, REAL_CALIBRATION) == Decimal("10.0")
    # X=417: a=0.142857142, b=-9.5714285   → ровно 50 л
    assert telemetry_service.fuel_litres(417, REAL_CALIBRATION) == Decimal("50.0")
    # и в середине отрезка, где интерполяция реально работает
    assert telemetry_service.fuel_litres(3608, REAL_CALIBRATION) == Decimal("572.6")


def test_calibration_covers_the_whole_tank():
    """Таблица покрывает бак целиком — иначе показания упрутся в потолок.

    С первыми шестью строками (до 417) боевые 3608 превращались в «50 л»
    вместо 573: значение зажималось краем таблицы.
    """
    assert len(REAL_CALIBRATION) == 34
    assert REAL_CALIBRATION[-1] == [3692, 585]
    # реальные показания из логов 25.08 попадают ВНУТРЬ таблицы, а не на край
    for raw in (3608, 3620, 3644):
        litres = telemetry_service.fuel_litres(raw, REAL_CALIBRATION)
        assert Decimal("560") < litres < Decimal("585"), f"{raw} → {litres}"


# ------------------------------------- новый терминал «СМАРТ» (навтелеком)
NEW_TERMINAL_PARAMS = {
    "power": 0.0, "power_reserv": 3.978, "fuel1": 3605, "temp_rs485_1": 17,
    "generator": 0, "jamming_gnss": 0, "gps_valid": 1, "pdop": 2.6,
    "odometer": 12.365753, "engineSeconds": 4956, "drain_sensor_ca": "idle",
}
OLD_TERMINAL_PARAMS = {
    "power": 25.11, "ignition": 1, "battery": 4.23,
    "fuel2": 65535, "fuelTemp2": -128,
}


def test_new_terminal_names_are_understood():
    """25.08 на Т557ОС178 сменился терминал, и поля поехали.

    Было (FleetGuide): fuel2 / fuelTemp2 / power=25.11 / ignition.
    Стало (навтелеком «СМАРТ»): fuel1 / temp_rs485_1 / power=0.0, ignition НЕТ.
    Владелец увидел «нужна тарировка», температуру прочерком и «нет данных»
    вместо напряжения — потому что имена другие.
    """
    assert telemetry_service.fuel_level_raw(NEW_TERMINAL_PARAMS) == Decimal("3605")
    # температура приезжает по RS-485, а не как fuelTemp
    assert telemetry_service.fuel_temp_c(NEW_TERMINAL_PARAMS) == Decimal("17")
    # старый терминал продолжает работать как работал
    assert telemetry_service.fuel_level_raw(OLD_TERMINAL_PARAMS) is None
    assert telemetry_service.fuel_temp_c(OLD_TERMINAL_PARAMS) is None


def test_engine_state_without_ignition_field():
    """У нового терминала поля ignition нет вообще — судим по generator.

    Иначе машина навсегда застревает в «стоит, зажигание не передано»:
    напряжение приходит нулём (масса выключена), а бита зажигания нет.
    """
    running = telemetry_service.engine_running_from_params
    assert running(NEW_TERMINAL_PARAMS) is False          # generator = 0
    assert running({**NEW_TERMINAL_PARAMS, "generator": 1}) is True
    # напряжение важнее флага: если бортсеть под генератором — двигатель работает
    assert running({"power": 28.1, "generator": 0}) is True
    # нет ни того ни другого — честное «не знаю», а не выдуманное False
    assert running({"fuel1": 100}) is None
    assert running(None) is None


def test_address_is_trimmed_for_the_card():
    """Nominatim отдаёт всё дерево — в карточке нужны первые составляющие."""
    from app.services.geocode_service import _short_address

    long = ("60 к6, Софийская улица, Обухово, Александровский округ, "
            "Санкт-Петербург, Северо-Западный федеральный округ, 192249, Россия")
    assert _short_address(long) == "60 к6, Софийская улица, Обухово"
    assert _short_address(None) is None


def test_parked_noise_is_not_consumption():
    """Стоящая машина не должна «расходовать» топливо.

    25.08 на реальной стоянке показания датчика гуляли в пределах ~4 литров без
    всякого движения. Если считать расход в лоб по разнице соседних точек, за
    сутки набежали бы десятки литров из воздуха.
    """
    t0 = datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc)
    noisy = [
        (t0 + timedelta(minutes=i), 3608 + (7 if i % 3 == 0 else -6))
        for i in range(60)
    ]
    summary = telemetry_service.fuel_summary(noisy, REAL_CALIBRATION)
    assert summary["spent_l"] == 0.0, summary
    assert summary["refuelled_l"] == 0.0
    assert summary["refuels"] == [] and summary["drains"] == []


def test_refuel_does_not_eat_the_consumption():
    """Заправка посреди периода не должна обнулять расход.

    Считаем спуски и подъёмы кривой отдельно: «конец минус начало» показал бы
    плюс, хотя машина реально сожгла топливо.
    """
    t0 = datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc)
    points, level = [], 2500
    for i in range(30):                      # едет, тратит
        level -= 20
        points.append((t0 + timedelta(minutes=3 * i), level))
    level += 900                             # заправился
    for i in range(30, 60):                  # снова едет
        level -= 20
        points.append((t0 + timedelta(minutes=3 * i), level))

    summary = telemetry_service.fuel_summary(points, REAL_CALIBRATION)
    assert summary["spent_l"] > 50, summary
    assert summary["refuelled_l"] > 50, summary
    assert len(summary["refuels"]) == 1


def test_sharp_drop_is_flagged_as_possible_drain():
    """Резкое падение уровня за минуты — повод проверить, а не расход."""
    t0 = datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc)
    points = [(t0 + timedelta(minutes=i), 2500) for i in range(10)]
    # −700 единиц (около 120 л) за пять минут
    points += [(t0 + timedelta(minutes=10 + i), 1800) for i in range(10)]
    summary = telemetry_service.fuel_summary(points, REAL_CALIBRATION)
    assert summary["drains"], "слив не помечен"
    assert summary["drains"][0]["litres"] > 50


def test_fuel_summary_needs_calibration_and_data():
    t0 = datetime(2026, 8, 26, 7, 0, tzinfo=timezone.utc)
    pts = [(t0, 3000), (t0 + timedelta(minutes=5), 2990)]
    assert telemetry_service.fuel_summary(pts, None) is None
    assert telemetry_service.fuel_summary([], REAL_CALIBRATION) is None
    # одной точки мало, чтобы говорить о расходе
    assert telemetry_service.fuel_summary([(t0, 3000)], REAL_CALIBRATION) is None


def test_smoothing_kills_single_spikes_not_the_trend():
    """Медиана, а не среднее: одиночный выброс не должен сдвигать кривую."""
    from decimal import Decimal as D
    series = [D(100), D(100), D(500), D(100), D(100)]
    assert telemetry_service.smooth_fuel(series)[2] == D(100)
    # настоящий спуск сглаживание сохраняет
    falling = [D(100), D(90), D(80), D(70), D(60)]
    assert telemetry_service.smooth_fuel(falling)[0] > telemetry_service.smooth_fuel(falling)[-1]


def test_calibration_text_is_parsed_forgivingly():
    """Владелец переписывает пары руками — принимаем любой разумный разделитель."""
    parse = telemetry_service.parse_fuel_calibration
    assert parse("1 0\n73,10\n168;20") == [[1.0, 0.0], [73.0, 10.0], [168.0, 20.0]]
    # порядок строк не важен, дубли схлопываются
    assert parse("168 20\n1 0\n168 21") == [[1.0, 0.0], [168.0, 21.0]]
    assert parse("") is None and parse(None) is None


def test_calibration_errors_say_what_is_wrong():
    """Молчаливое «не сохранилось» — худший ответ. Ошибка называет строку."""
    parse = telemetry_service.parse_fuel_calibration
    for bad, expect in (("1", "Строка 1"), ("абв где", "Строка 1"), ("1 0", "минимум две")):
        try:
            parse(bad)
            raise AssertionError(f"«{bad}» приняли, а не должны были")
        except ValueError as exc:
            assert expect in str(exc), str(exc)


def test_invalid_gps_ring_is_dashed_everywhere():
    """«GPS недостоверный» рисуется пунктиром во ВСЕХ трёх местах.

    Владелец 24.08: «в легенде написано пунктиром, а рисуется не пунктиром».
    Пунктир был только у метки на карте: в строке списка и в карточке рамка
    задавалась через box-shadow, а он пунктир не умеет. Теперь для этого
    состояния переходим на border.
    """
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    assert "function ringCss(" in src and "function applyRing(" in src
    assert "stateKey === 'invalid'" in src
    assert "px dashed ' + color" in src
    # ни одно из трёх мест не задаёт кольцо в обход общей функции
    assert "box-shadow: 0 0 0 2px ' + st.color" not in src, "строка списка мимо ringCss"
    assert "boxShadow = '0 0 0 2.5px ' + st.color" not in src, "карточка мимо applyRing"
    assert "ringCss(key, stateColor(key), 2)" in src
    assert "applyRing(document.getElementById('mon-card-tile')" in src
    assert "applyRing(tile, key, stateColor(key), 2.5" in src
    # рамка не должна менять размер плитки
    assert src.count("box-sizing: border-box; border: 0;") >= 2
    # у метки внутреннее кольцо гасим, иначе два пунктира друг на друге
    assert '.mon-pin[data-state="invalid"] .mon-pin__ring { border-color: transparent; }' in src


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


# ------------------------------------------------------------------ геозоны
def test_geofence_default_radius_is_one_named_constant():
    """Базовый радиус — 400 м и живёт в одном месте.

    Владелец 21.08 принял за баг то, что у всех зон на карте написано «400 м»:
    пустое поле «Радиус» и означает базовые 400. Теперь подпись говорит,
    задан радиус вручную или взят базовый.
    """
    from app.services.rc_service import RC_DEFAULT_RADIUS_M

    assert RC_DEFAULT_RADIUS_M == 400
    router = open("app/web/router.py", encoding="utf-8").read()
    assert "rc_service.RC_DEFAULT_RADIUS_M" in router
    assert '"radius_custom": rc.geofence_radius_m is not None' in router
    screen = open("app/web/templates/map.html", encoding="utf-8").read()
    assert "задан вручную" in screen
    assert "базовый (поле «Радиус» у склада пустое)" in screen


# ------------------------------------------------------------------ адрес
def test_reverse_geocode_parsers():
    from app.services import geocode_service as g

    yandex = {"response": {"GeoObjectCollection": {"featureMember": [
        {"GeoObject": {"metaDataProperty": {"GeocoderMetaData": {
            "text": "Россия, Санкт-Петербург, Софийская улица, 60к7"}}}}]}}}
    assert g.parse_yandex_reverse(yandex) == "Санкт-Петербург, Софийская улица, 60к7"
    assert g.parse_nominatim_reverse(
        {"display_name": "Россия, Санкт-Петербург, Софийская улица"}
    ) == "Санкт-Петербург, Софийская улица"
    # мусор и пустые ответы не должны падать
    for bad in ({}, [], None, "текст", {"response": {}}):
        assert g.parse_yandex_reverse(bad) is None
    for bad in ([], None, "текст", {}):
        assert g.parse_nominatim_reverse(bad) is None


def test_reverse_geocode_endpoint_is_cached_and_throttled():
    src = open("app/web/router.py", encoding="utf-8").read()
    assert '@app.get("/api/geocode/reverse")' in src
    # кэш по округлённым координатам: стоящая машина не дёргает сервис
    assert "_GEOCODE_CACHE" in src and '{lat:.4f},{lon:.4f}' in src
    # пауза между запросами — политика Nominatim
    assert "_GEOCODE_MIN_GAP" in src
    # координаты проверяются, чужие значения в внешний сервис не уходят
    assert "Bad coordinates" in src


# ------------------------------------------------------------------ напряжение
def test_locations_api_falls_back_to_last_point_voltage():
    """Если в «быстром слое» напряжения нет, берём его из свежей точки.

    Так карточка показывает напряжение даже когда приёмник телеметрии ещё
    не обновлён или состояние обновилось недостоверной точкой.
    """
    src = open("app/web/router.py", encoding="utf-8").read()
    assert "missing_voltage" in src
    assert "VehicleTelemetryPoint.voltage.is_not(None)" in src
    screen = open("app/web/templates/map.html", encoding="utf-8").read()
    # пустое напряжение подписано словами, а не голым прочерком
    assert "нет данных" in screen


def test_receiver_logs_what_it_actually_stored():
    """В лог пишем сохранённое напряжение, а не только пришедшее в params."""
    src = open("app/telemetry/egts_receiver.py", encoding="utf-8").read()
    assert "stored_voltage=%s" in src
    assert "last_good.voltage if last_good is not None" in src


def test_monitoring_screen_has_track_camera_and_voltage():
    """Ключевые куски экрана мониторинга на месте."""
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    # трек грузится с нашего эндпоинта
    assert "/api/vehicles/' + vehicleId + '/track?hours=" in src
    # разрыв связи рисуется пунктиром, а не сплошной
    assert "dash: [7, 6]" in src
    assert "#c5362b" in src
    # камера едет за выбранной машиной и отпускает карту, если её тронули
    assert "setFollow(selected)" in src
    assert "mousedown" in src and "setFollow(null)" in src
    # напряжение бортсети в карточке
    assert "Напряжение" in src
    # метка уменьшается на общем плане
    assert "pinSizeForZoom" in src


# ------------------------------------------------------------------ карта 3.0
def test_map_never_hands_raw_lat_lon_to_yandex_3_0():
    """В JS API 3.0 координаты — [долгота, широта], наоборот к 2.1 и к серверу.

    Самая дорогая ошибка переноса: где-то забыли перевернуть пару — метка
    уезжает в другое полушарие, и глазами по коду это не ловится. Поэтому
    проверяем механически: КАЖДОЕ место, где координаты уходят в карту,
    названо через общий конвертер.
    """
    import re

    src = open("app/web/templates/map.html", encoding="utf-8").read()
    assert "function LL(lat, lon) { return [lon, lat]; }" in src
    assert "function LLp(p) { return [p[1], p[0]]; }" in src
    allowed = ("LL(", "LLp(", "LLline(", "boundsOf(", "[circleRing(",
               "c.coords", "lngLat")
    for field in ("coordinates:", "center:", "bounds:"):
        for m in re.finditer(re.escape(field) + r"\s*([^,\n}]+)", src):
            value = m.group(1).strip()
            assert value.startswith(allowed), f"{field} {value} — мимо конвертера"


def test_map_bounds_use_top_left_and_bottom_right():
    """LngLatBounds в 3.0 — это [верхний левый, нижний правый].

    Не «юго-запад, северо-восток», как в большинстве других карт: перепутать
    углы = камера уедет мимо машин. Порядок сверен с типами @yandex/ymaps3-types.
    """
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    assert "return [[minLon, maxLat], [maxLon, minLat]];" in src


def test_map_migrated_off_2_1_api():
    """Ни одного вызова 2.1 не осталось — иначе карта молча потеряет часть меток."""
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    for banned in ("ymaps.Map", "ymaps.ready", "ymaps.Placemark", "ymaps.Polyline",
                   "ymaps.Circle", "templateLayoutFactory", "geoObjects",
                   "iconLayout", "hintContent", "balloonContentBody",
                   "setBounds", "fitToViewport", "api-maps.yandex.ru/2.1/"):
        assert banned not in src, f"осталось от 2.1: {banned}"
    assert "ymaps3.ready" in src
    assert "new YMapDefaultFeaturesLayer" in src, "без этого слоя меток не будет видно"


def test_map_has_dark_theme_switch():
    """Тёмная карта — то, ради чего переезжали: в 2.1 её нет вообще."""
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    # тема отдаётся самой карте и запоминается между заходами
    assert "ymap.update({ theme: theme })" in src
    assert "localStorage.setItem(THEME_KEY, theme)" in src
    # панели темнеют тем же переключателем, одним блоком стилей
    assert 'mon.dataset.mode = theme' in src
    assert '.mon[data-mode="dark"] {' in src


def test_dark_map_theme_is_set_on_the_scheme_layer():
    """Тему ТАЙЛОВ задаёт слой схемы, а не свойство theme у карты.

    26.08 на боевом было ровно так: панели потемнели, а карта под ними
    осталась светлой. Выданная Яндексом сборка 3.0 молча игнорирует theme
    у YMap; работает только theme в конструкторе YMapDefaultSchemeLayer —
    этот путь и был проверен на боевом ключе.
    """
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    assert "new YMapDefaultSchemeLayer({ theme: theme })" in src
    assert "function applyMapTheme()" in src
    # переключатель зовёт ту же функцию, а не красит панели в обход карты
    assert src.count("applyMapTheme()") >= 2
    # состояния на тёмных тайлах ярче и перекрашиваются вслед за темой
    assert "STATE_COLORS_DARK" in src
    assert "function stateColor(key)" in src
    assert "st.color" not in src, "где-то остался цвет состояния мимо stateColor"
    # цвет в легенде живёт в стилях: инлайновый стиль тёмная тема не перебьёт
    assert 'data-state="invalid"><i></i>' in src


def test_markers_stay_above_the_basemap():
    """Слой меток обязан лежать НАД подложкой, иначе спутник её накрывает.

    26.08 на боевом: включаешь «Спутник» — и машины исчезают с карты. Причина
    не в спутнике, а в zIndex: по умолчанию любой слой в 3.0 идёт с 1500, ровно
    как подложка, а при равенстве выигрывает добавленный позже.
    """
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    assert "new YMapDefaultFeaturesLayer({ zIndex: 1800 })" in src
    # и порядок потомков: после любой смены подложки слой меток поднимается
    assert "function raiseFeatures()" in src
    assert src.count("raiseFeatures()") >= 3


def test_dark_theme_covers_panels_that_paint_background_by_number():
    """Панели, у которых фон вписан числом, обязаны иметь тёмную пару.

    Панель трека такую пару потеряла при переносе палитры на токены макета:
    получилась белая плашка со светлым текстом — то есть пустое место.
    """
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    assert 'background: rgba(13, 21, 36, .88);\n  border-color: rgba(255, 255, 255, .09);' in src
    # светлый акцент тёмной темы нельзя пускать под белый текст
    assert '.mon[data-mode="dark"] .mon-follow,' in src
    assert "background: #2563eb; color: #fff; border-color: #2563eb;" in src


def test_map_says_out_loud_when_the_key_is_rejected():
    """Ключ не приняли — пишем это словами, а не показываем серый прямоугольник.

    2.1 отдавала библиотеку на ЛЮБОЙ ключ и ругалась уже внутри карты — из-за
    этого опечатку в ключе искали три дня (PROBLEMS.md №23). 3.0 проверяет ключ
    на выдаче, значит можно сказать владельцу, что именно произошло.
    """
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    assert "typeof ymaps3 === 'undefined'" in src
    assert "Карта не загрузилась" in src
    assert "Invalid api key" in src
    assert ".mon-mapfail" in src


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


def test_fuel_period_is_named_by_hours_not_by_the_word_day():
    """«Расход за сутки» читалось как «за календарные сутки» или «за всё время».

    На деле окно скользящее: всегда последние 24 часа от текущего момента,
    в полночь оно не обнуляется. Владелец 27.08.2026 засомневался в цифре
    именно из-за названия — пишем число часов, а не слово «сутки».
    """
    src = open("app/web/templates/map.html", encoding="utf-8").read()
    assert "Расход за 24 часа" in src
    assert "Расход за сутки" not in src
    # и запрашиваем ровно те же 24 часа, что обещаем в подписи
    assert "/fuel?hours=24" in src

    router = open("app/web/router.py", encoding="utf-8").read()
    assert "since = datetime.now(timezone.utc) - timedelta(hours=hours)" in router
