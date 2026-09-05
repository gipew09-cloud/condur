# Запросы для генерации картинок и иконок

Составлено 05.09.2026 по просьбе владельца: «напиши точный запрос для
генерации иконок». Здесь два блока — **машины** (генерировать имеет смысл) и
**иконки интерфейса** (сначала прочитайте предупреждение).

---

## ⚠️ Про иконки: сначала прочитайте, потом решайте

Иконки интерфейса нейросетью получаются плохо, и вот почему:

- генератор картинок отдаёт **PNG, а не SVG** — на экране телефона такая
  иконка мылится, а мы её ещё и перекрашиваем под тёмную тему;
- **толщина линий гуляет** от иконки к иконке, и набор перестаёт выглядеть
  набором: одна пожирнее, другая потоньше, третья с другим скруглением;
- мелкие детали (стрелка на спидометре, буква P) в размере 20 пикселей
  превращаются в пятно.

**В проекте уже подключён набор Phosphor Icons** — тысяча значков в одном
стиле, векторные, красятся одной строкой, ничего скачивать не нужно. Нужные
нам значки там есть все:

| Что показываем | Значок Phosphor |
|---|---|
| Топливо | `ph-drop` |
| Температура топлива | `ph-thermometer-simple` |
| Скорость | `ph-speedometer` |
| Напряжение бортсети | `ph-lightning` |
| Координаты | `ph-crosshair` |
| Зажигание | `ph-key` |
| Стоянка | `ph-car-profile` / `ph-park` |
| Завёл двигатель | `ph-engine` |
| Заглушил двигатель | `ph-power` |
| Превышение скорости | `ph-timer` |
| Заправка | `ph-gas-pump` |
| Слив топлива | `ph-drop-half` |
| Геозона (РЦ) | `ph-factory` |
| Тревога | `ph-warning` |
| Начало пути | `ph-play` |
| Конец пути | `ph-flag-checkered` |

Скажите слово — подключу их за один заход, бесплатно и в одном стиле.
Свои иконки имеет смысл рисовать позже, когда захочется фирменного вида, и
тогда лучше **не генерировать, а нарисовать в векторе** (Figma) по тем же
правилам, что ниже.

---

## Если всё-таки генерировать иконки

**Общая часть — приклеивать к КАЖДОМУ запросу без изменений** (иначе набор
получится разностильным):

```
minimal flat line icon, single color #1D4ED8 on fully transparent background,
drawn on a 24x24 grid with 2px uniform stroke, rounded caps and joins,
geometric and simple, no gradients, no shadows, no text, no frame,
centered with 2px padding, professional UI icon set style, PNG with alpha,
square 512x512
```

**И к нему — одна строка на иконку** (по одной картинке за раз, как вы и
хотели):

| Файл | Строка запроса |
|---|---|
| `fuel.png` | `a fuel drop icon` |
| `temp.png` | `a simple thermometer icon` |
| `speed.png` | `a speedometer gauge icon with a needle` |
| `voltage.png` | `a lightning bolt icon` |
| `coords.png` | `a crosshair target icon` |
| `ignition.png` | `a car key icon` |
| `parking.png` | `a rounded square with the letter P inside` |
| `engine-on.png` | `a car engine block icon` |
| `engine-off.png` | `a power button icon, circle with a vertical line` |
| `speeding.png` | `a stopwatch icon` |
| `refuel.png` | `a fuel pump icon` |
| `drain.png` | `a jerrycan with a drop falling from it` |
| `geozone.png` | `a warehouse building icon` |
| `alarm.png` | `a triangle with an exclamation mark inside` |
| `start.png` | `a play triangle inside a circle` |
| `finish.png` | `a checkered finish flag` |

Требования к файлам, которые вы пришлёте: **PNG с прозрачным фоном**,
квадрат, один цвет, без подписей. Если получится SVG — присылайте SVG,
это лучше во всём.

---

## Машины на карту — вот это генерировать стоит

⚠️ **Главное требование, без него ничего не заработает:** кузов должен быть
**светлым** (белый или очень светло-серый). Кабинет перекрашивает машину в
цвет, который вы выбрали на странице машины, и трогает только светлые
пиксели — тёмные (колёса, рама, стёкла, решётка холодильника) остаются как
нарисованы. Тёмную картинку перекрасить нельзя: получится одноцветная клякса.

**Общая часть — приклеивать к каждому запросу:**

```
flat vector illustration, strict side view at eye level, white and very light
grey body, dark grey wheels and windows, clean simple shapes, thin outlines,
fully transparent background, no shadow, no reflection, no ground, no text,
no logos, no license plates, no background objects, centered, wide image
1600x600, PNG with alpha
```

**И одна строка на каждую машину:**

| Файл | Строка запроса |
|---|---|
| `truck-reefer.png` | `a semi truck with a refrigerated box trailer` |
| `truck-tent.png` | `a truck with a curtain-side tilt trailer` |
| `truck-van.png` | `a small delivery van, Gazelle type` |

Что проверить перед отправкой мне:

1. фон **прозрачный**, а не белый (в белом фоне метка станет квадратом);
2. машина смотрит **влево или вправо**, ровно сбоку, без наклона;
3. кузов светлый, колёса тёмные;
4. никаких теней и «пола» под колёсами — тень рисует кабинет сам;
5. один файл — одна машина, без подписей.

Файлы кладите в `app/web/static/` (там уже лежит `truck-reefer.png`) или
просто скажите, где они, — подключу.
