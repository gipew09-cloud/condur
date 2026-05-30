# TMS MVP — операционная система владельца малого автопарка

Стартовый скелет. Цель шага 1: поднять окружение, создать таблицы в базе
и убедиться, что оба бота отвечают на `/start`.

## Что внутри

```
tms-mvp/
├── docker-compose.yml      # Postgres + Redis (инфраструктура)
├── requirements.txt        # зависимости Python
├── .env.example            # пример настроек (скопировать в .env)
├── create_db.py            # создаёт таблицы из моделей
└── app/
    ├── config.py           # чтение настроек из .env
    ├── database.py         # подключение к базе
    ├── models.py           # таблицы в виде Python-классов
    ├── main.py             # запуск обоих ботов
    └── bots/
        ├── owner_bot.py    # бот владельца
        └── driver_bot.py   # бот водителя
```

## Шаги запуска (по порядку)

### 1. Создай двух ботов в Telegram
Напиши @BotFather → `/newbot` дважды. Получишь два токена — для бота
владельца и бота водителя.

### 2. Настрой .env
```bash
cp .env.example .env
```
Открой `.env` и впиши оба токена.

### 3. Подними базу и Redis
Нужен установленный Docker.
```bash
docker compose up -d
```
Проверить, что поднялись: `docker compose ps`

### 4. Поставь зависимости Python
Лучше в виртуальном окружении:
```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 5. Создай таблицы
```bash
python create_db.py
```
Должно вывести: `✅ Таблицы созданы.`

### 6. Запусти ботов
```bash
python -m app.main
```
Теперь напиши `/start` каждому боту в Telegram — оба должны ответить.
Остановить: `Ctrl+C`.

## Что дальше (шаг 2)
Регистрация владельца в боте + добавление водителей (инвайт-ссылка) и машин,
с записью в базу.
