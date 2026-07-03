# Stavtrack / EGTS: инструкция подключения

## Что уже есть в коде

В проект добавлен единый Railway-запуск:

```bash
python -m app.run
```

Он сам выбирает, что запускать:

- без `SERVICE_ROLE` или с `SERVICE_ROLE=main` — основной `condur`: миграции, боты, веб;
- с `SERVICE_ROLE=egts` — только TCP-приёмник Stavtrack.

Сам TCP-приёмник можно запустить напрямую так:

```bash
python -m app.telemetry.egts_receiver
```

Это не Telegram-бот и не веб-кабинет. Он только принимает данные от Stavtrack
по TCP/EGTS и сохраняет сырые пакеты в таблицу `vehicle_telemetry_raw_packets`.

Также добавлены таблицы:

- `vehicle_telemetry_raw_packets` — сырые пакеты от Stavtrack.
- `vehicle_telemetry_points` — будущие разобранные GPS-точки.
- `vehicle_states` — будущее последнее состояние машины.
- `vehicles.stavtrack_object_id` — ID машины из Stavtrack.

ID можно заполнить в веб-кабинете на странице `/vehicles`: открыть машину на редактирование
и вписать число из правой колонки Stavtrack, например `129772`.

## Что делать с сервисом `confident-cooperation`

Его можно не удалять, если использовать как GPS-приёмник.

Главное: не запускать его с обычной командой `app.main`, иначе это будет попытка
запустить второй экземпляр основного приложения.

Правильнее:

1. Переименовать сервис в Railway в `egts-receiver`.
2. Start Command оставить из `railway.json`:

```bash
python -m app.run
```

3. В Variables добавить:

```env
SERVICE_ROLE=egts
DATABASE_URL=<тот же Postgres, что у condur>
EGTS_PORT=9000
EGTS_MAX_PACKET_BYTES=65536
EGTS_IDLE_TIMEOUT_SECONDS=120
```

Telegram-токены в этот сервис добавлять не нужно.

## Railway: что нажимать

1. Сначала задеплоить основной `condur`, чтобы прошли миграции базы.
2. Открыть сервис `egts-receiver`.
3. Проверить Start Command:

```bash
python -m app.run
```

4. В Variables у `egts-receiver` должно быть `SERVICE_ROLE=egts`.
5. Settings -> Networking -> Public Networking -> `+ TCP Proxy`.
6. Internal Port указать:

```text
9000
```

7. Railway выдаст адрес и порт вида:

```text
something.proxy.rlwy.net:12345
```

Именно их нужно вставить в Stavtrack.

## Stavtrack: что указывать

В разделе ретрансляций:

- Протокол: `egts`
- Канал: `TCP`
- Адрес: домен Railway без порта, например `something.proxy.rlwy.net`
- Порт: порт Railway, например `12345`
- Объекты: сначала выбрать только 1 машину
- Включено: да

Не подключать сразу все машины. Сначала проверяем одну.

Перед тестом желательно в кабинете проекта открыть `Машины` и у этой машины заполнить
`Stavtrack ID`, чтобы потом связать поток с конкретным госномером.

## Как понять, что данные пошли

В Railway у сервиса `egts-receiver` в Logs должны появиться строки вида:

```text
EGTS connection opened
EGTS raw saved: id=... bytes=...
```

В базе начнут появляться записи в `vehicle_telemetry_raw_packets`.

## Важное ограничение первого этапа

Сейчас приёмник намеренно сохраняет сырые EGTS-данные без разбора координат.
Это нужно, чтобы увидеть реальные пакеты Stavtrack. После этого добавляется
парсер EGTS, ACK-ответы и логика:

- где машина сейчас;
- включено ли зажигание;
- едет ли машина без активного рейса;
- сколько было подозрительных километров;
- какие GPS-точки не считаем из-за глушения/скачков.

## Если Railway TCP будет плохо доступен из России

Тогда схема меняется на более устойчивую:

```text
Stavtrack -> российский VPS -> Railway HTTPS
```

Railway оставляем как основную систему, а VPS будет маленьким шлюзом с обычным
российским IP.
