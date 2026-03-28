# 🤖 Redmine → Matrix Notification Bot

Бот автоматически отслеживает изменения в задачах Redmine и отправляет уведомления в Matrix-чат (Element / Synapse / любой Matrix-клиент).

### Актуальная схема (репозиторий `matrix_bot_firebeard`)

| Что | Как сейчас |
|-----|------------|
| Запуск | **`python bot.py`** из корня репозитория (файл **`bot.py`**, не `src/bot.py`) |
| Пользователи и маршруты | JSON в **`.env`**: `USERS`, `STATUS_ROOM_MAP`, `VERSION_ROOM_MAP` |
| State | JSON **`state_<redmine_id>_*.json`** в каталоге проекта (рядом с `bot.py`) |
| Интервал опроса Redmine | По умолчанию **90 с** (`CHECK_INTERVAL` в `bot.py`) |
| Matrix | Отправка с **`room_send_with_retry`**: до **3** попыток, паузы **1 с** и **2 с** |
| `src/preferences.py` (DND / рабочие часы) | Модуль есть; **в `bot.py` не вызывается** — уведомления без этого фильтра |

Разделы ниже про **`config.yaml`**, **91 тест** и пути вроде `data/` относятся к целевой/альтернативной схеме и постепенно приводятся к виду выше.

---

## Возможности

| № | Функция | Описание |
|---|---------|----------|
| 1 | **Новые задачи** | Уведомление при появлении новой назначенной задачи |
| 2 | **Смена статуса** | Уведомление при изменении статуса задачи |
| 3 | **Комментарии и изменения** | Отслеживание через Redmine journals API — автор и тип изменения |
| 4 | **Просроченные задачи** | Ежедневное уведомление с количеством дней просрочки |
| 5 | **Напоминания** | По статусу «Информация предоставлена» — периодически (интервал в коде) |
| 6 | **Информация предоставлена** | Уведомление при переходе в этот статус |
| 7 | **Маршрутизация** | Разные статусы / версии / команды → разные комнаты Matrix |
| 8 | **Мультипользовательность** | Поддержка нескольких пользователей с индивидуальными настройками |
| 9 | **Рабочие часы и DND** | Заложено в `src/preferences.py`; **подключение к `bot.py` — в планах** |

---

## Архитектура

```
┌──────────┐   REST API (~каждые 90с)   ┌──────────────┐  Matrix C-S API   ┌──────────┐
│  Redmine │ ◄───────────────────────── │   bot.py     │ ────────────────► │  Matrix  │
│  (задачи)│                            │ APScheduler  │                   │  (чат)   │
└──────────┘                            └──────┬───────┘                   └──────────┘
                                               │
                                        ┌──────▼───────┐
                                        │ state_*.json │
                                        │ (корень)     │
                                        └──────────────┘
```

**Цикл работы:**
1. Планировщик вызывает проверку каждые `CHECK_INTERVAL` секунд (по умолчанию 90).
2. Для каждого пользователя загружаются открытые назначенные задачи из Redmine.
3. Текущее состояние сравнивается с сохранённым в `state_<uid>_*.json`.
4. При обнаружении изменений — HTML-уведомление собирается и отправляется в Matrix (с повторами при сбое).
5. Новое состояние сохраняется атомарно в JSON.

---

## Стек технологий

| Компонент | Технология | Назначение |
|-----------|-----------|------------|
| Язык | Python 3.11+ | Основной язык |
| Matrix API | `matrix-nio` | Async-отправка HTML-сообщений в чат |
| Redmine API | `python-redmine` | Получение задач, журналов, статусов |
| Планировщик | `APScheduler` | Периодические проверки |
| Конфигурация | `python-dotenv` | Секреты и JSON-маппинги из `.env` |
| Тестирование | `pytest` + `pytest-asyncio` | ~190+ тестов (`tests/`) |
| Процесс-менеджер | systemd | Автозапуск, перезапуск при сбоях |

---

## Структура проекта

```
matrix_bot_firebeard/
├── bot.py                 # Точка входа: Redmine + Matrix + APScheduler
├── bot.log                # Лог (ротация в коде)
├── state_<uid>_*.json     # Состояние по пользователям Redmine (рядом с bot.py)
├── requirements.txt
├── README.md
├── .env                   # Секреты — не коммитить
├── src/                   # Общие модули и задел под рефакторинг
│   ├── config.py
│   ├── utils.py           # в т.ч. safe_html()
│   ├── matrix_client.py
│   ├── state.py
│   ├── preferences.py
│   └── ...
└── tests/                 # pytest: test_bot.py + модули src/
    └── conftest.py
```

### Модули `src/` — краткое описание

| Модуль | Назначение | Тесты |
|--------|-----------|:-----:|
| `config.py` | Загрузка `.env`, пути, приоритеты, статусы Redmine, `validate_config()` | 15 |
| `utils.py` | `now()`, `safe_html()`, `truncate_text()`, timezone | 25 |
| `state.py` | `load_state()` / `save_state()` — JSON с атомарной записью | 14 |
| `preferences.py` | Рабочие часы, DND, `can_notify()` с Emergency bypass | 27 |
| `matrix_client.py` | Singleton `AsyncClient`, access_token, retry × 3, `send_message()` | 10 |
| `redmine_checks.py` | Основная логика проверки задач | — |
| `routing.py` | Выбор комнаты Matrix по статусу/версии задачи | — |
| `commands.py` | Интерактивные команды бота | — |
| `onboarding.py` | Регистрация новых пользователей | — |
| `reports.py` | Генерация отчётов | — |

---

## Быстрый старт

### 1. Клонирование и окружение

```bash
git clone git@github.com:forgebeard/redmine-matrix-bot.git
cd redmine-matrix-bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Настройка секретов

```bash
cp .env.example .env
nano .env   # заполнить все переменные (см. раздел «Настройка .env»)
```

### 3. Настройка пользователей и маршрутизации

Задайте JSON в **`.env`** (переменные `USERS`, при необходимости `STATUS_ROOM_MAP`, `VERSION_ROOM_MAP`). Пример см. в разделе «Настройка пользователей» ниже.

### 4. Проверка конфигурации (модуль `src/`)

```bash
cd src && python3 -c "from config import validate_required_env; ok, m = validate_required_env(); print('OK' if ok else m)"
```

### 5. Запуск тестов

```bash
python -m pytest tests/ -v --tb=short
```

### 6. Тестовый запуск бота

```bash
python3 bot.py
```

Если в логе видно `✅ Matrix: ...` и `✅ Redmine: ...` — бот работает. Остановить — `Ctrl+C`.

### 7. Установка как systemd-сервис

```bash
nano redmine-matrix-bot.service   # указать пути и пользователя

sudo cp redmine-matrix-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable redmine-matrix-bot
sudo systemctl start redmine-matrix-bot
```

---

## Настройка .env

```env
# ─── Matrix-сервер ───────────────────────────────────
MATRIX_HOMESERVER=https://messenger.example.com
MATRIX_ACCESS_TOKEN=syt_your_access_token_here
MATRIX_USER_ID=@bot_user:messenger.example.com
MATRIX_DEVICE_ID=BOTDEVICE

# ─── Redmine ─────────────────────────────────────────
REDMINE_URL=https://redmine.example.com
REDMINE_API_KEY=your_redmine_api_key
```

### Где взять токены

| Параметр | Где получить |
|----------|-------------|
| `MATRIX_ACCESS_TOKEN` | Element → Настройки → Помощь и О программе → Access Token |
| `MATRIX_USER_ID` | Формат: `@username:your.server` |
| `MATRIX_DEVICE_ID` | Любой идентификатор (например, `BOTDEVICE`) |
| `REDMINE_API_KEY` | Redmine → Моя учётная запись → API-ключ (правая колонка) |

> ⚠️ **Важно:** Бот использует `access_token` (не пароль) для авторизации в Matrix — это безопаснее и не требует login-flow.

> ⚠️ **Важно:** API-ключ Redmine определяет, чьи задачи бот может видеть. Рекомендуется использовать ключ с правами администратора, если бот обслуживает нескольких пользователей.

---

## Настройка пользователей (YAML-пример для справки)

> **Сейчас бот читает пользователей из JSON в `.env` (`USERS`), а не из файла `config.yaml`.** Ниже — ориентир по полям; при необходимости тот же смысл переносится в JSON в `USERS`.

Фрагмент ниже иллюстрирует **пользователей**, **типы уведомлений** и **маршрутизацию** в комнаты Matrix (целевая схема).

```yaml
users:
  # ─── Пользователь 1: получает ВСЕ уведомления в одну комнату ───
  1972:                                              # Redmine user ID
    matrix_user: "@user1:messenger.example.com"      # Matrix-аккаунт пользователя
    default_room: "!room_id:messenger.example.com"   # Комната по умолчанию
    notify: ["all"]                                   # Все типы уведомлений

  # ─── Пользователь 2: выборочные уведомления + маршрутизация ───
  3254:                                              # Redmine user ID
    matrix_user: "@user2:messenger.example.com"
    default_room: "!room_id2:messenger.example.com"
    notify: ["new", "info", "issue_updated"]          # Только выбранные типы

    # Маршрутизация по статусу задачи:
    status_routes:
      - status: "Передано в работу"
        room: "!team_room:messenger.example.com"

    # Маршрутизация по версии (продукту):
    version_routes:
      - match: "Продукт А"
        room: "!product_a_room:messenger.example.com"
      - match: "Продукт Б"
        room: "!product_b_room:messenger.example.com"

    # Комната команды (опционально)
    team_room: "!team_room:messenger.example.com"
```

### Как узнать Redmine user ID

Откройте профиль пользователя в Redmine — ID будет в URL:
```
https://redmine.example.com/users/1972
                                 ^^^^
```

### Как узнать Matrix Room ID

В Matrix-клиенте (Element / др.): Настройки комнаты → Дополнительно → Внутренний ID комнаты.

Формат: `!случайная_строка:ваш.сервер`

### Типы уведомлений (notify)

| Тип | Описание |
|-----|----------|
| `all` | Все типы сразу (шорткат) |
| `new` | Новая задача появилась в списке назначенных |
| `status_change` | Статус задачи изменился |
| `issue_updated` | Новый комментарий или изменение в журнале задачи |
| `info` | Статус задачи = «Запрос информации» |
| `overdue` | Дедлайн задачи прошёл (уведомление раз в сутки) |
| `reminder` | До дедлайна ≤ 3 дней (уведомление раз в сутки) |

### Приоритет маршрутизации

Бот выбирает комнату в следующем порядке (первое совпадение):

1. **status_routes** — если статус задачи совпадает
2. **version_routes** — если версия задачи содержит подстроку
3. **team_room** — комната команды
4. **default_room** — комната по умолчанию (fallback)

---

## State-файлы

Бот хранит состояние в JSON-файлах в директории `data/` (вместо БД — для простоты и переносимости):

| Файл | Назначение |
|------|-----------|
| `data/state_{uid}_sent.json` | Задачи, о которых уже уведомили + их текущий статус |
| `data/state_{uid}_journals.json` | Последний `journal_id` — для отслеживания новых изменений |
| `data/state_{uid}_overdue.json` | Дата последнего уведомления о просрочке |
| `data/state_{uid}_reminders.json` | Дата последнего напоминания о дедлайне |

`{uid}` — Redmine user ID из `config.yaml`.

Директория `data/` создаётся автоматически при первом запуске. Запись файлов — **атомарная** (через temp-файл + rename), что предотвращает повреждение данных при внезапном отключении.

> 💡 При **первом запуске** бот НЕ отправляет уведомления — только запоминает текущее состояние всех задач. Это предотвращает спам при начальной инициализации.

### Сброс состояния

```bash
# Полный сброс — бот заново запомнит состояние (без спама при первом цикле)
rm -f data/state_*.json
sudo systemctl restart redmine-matrix-bot

# Сброс только journals — при следующем цикле обработает все журналы как новые
rm -f data/state_*_journals.json
sudo systemctl restart redmine-matrix-bot
```

---

## Рабочие часы и DND

Бот поддерживает режим рабочего времени и «Не беспокоить»:

| Параметр | По умолчанию | Описание |
|----------|:------------:|----------|
| Рабочие часы | 09:00–18:00 | Уведомления отправляются только в этот интервал |
| Рабочие дни | Пн–Пт | Выходные = тишина |
| DND | выкл. | Пользователь может включить вручную |
| Emergency | всегда | Приоритет «Немедленный» пробивает любой DND и нерабочее время |

---

## Формат уведомлений

Все уведомления отправляются в HTML (`org.matrix.custom.html`) с кликабельными ссылками. Тексты из Redmine (тема, статусы, журнал) проходят через **`safe_html()`** из `src/utils.py` при сборке сообщений в `bot.py`.

### 🆕 Новая задача

```
🆕 Новая задача

#63603 — Настройка сервера

Статус: Новая
Приоритет: Нормальный
Версия: Продукт 2.0
📅 Срок: 2026-04-01

🔗 Открыть задачу
```

### 📝 Задача обновлена

```
📝 Задача обновлена

#63603 — Настройка сервера

Статус: Информация предоставлена
Приоритет: Нормальный
Версия: Продукт 2.0

💬 Комментарий от Иванов Иван
Смена статуса: В работе → Информация предоставлена

🔗 Открыть задачу
```

### ⚠️ Просроченная задача

```
⚠️ Просроченная задача

#55339 — Обновление документации

Статус: В работе
Приоритет: Низкий
📅 Срок: 2025-12-19 (просрочено на 98 дней)

🔗 Открыть задачу
```

### 🔄 Смена статуса

```
🔄 Статус изменён

#63603 — Настройка сервера

Статус: Информация предоставлена (было: В работе)
Приоритет: Нормальный

🔗 Открыть задачу
```

### ⏰ Напоминание о дедлайне

```
⏰ Скоро дедлайн

#63603 — Настройка сервера

Статус: В работе
📅 Срок: 2026-03-30 (через 3 дня)

🔗 Открыть задачу
```

### Сводная таблица типов

| Эмодзи | Тип | Когда срабатывает |
|--------|-----|-------------------|
| 🆕 | Новая задача | Задача появилась в списке назначенных |
| 🔄 | Смена статуса | Статус задачи изменился |
| 📝 | Обновление | Новый комментарий или изменение в журнале |
| ⚠️ | Просрочка | `due_date` < сегодня (раз в сутки) |
| ⏰ | Напоминание | До `due_date` ≤ 3 дней (раз в сутки) |
| ℹ️ | Запрос информации | Статус = «Запрос информации» |

---

## Тестирование

В проекте **сотни тестов** в `tests/` (корневой `test_bot.py` и модули `src/`).

```bash
# Все тесты
python -m pytest tests/ -v --tb=short

# Один модуль
python -m pytest tests/test_preferences.py -v

# С покрытием (если установлен pytest-cov)
python -m pytest tests/ --cov=src --cov-report=term-missing
```

| Область | Файлы | Что проверяется |
|--------|-------|-----------------|
| Корневой бот | `test_bot.py` | Логика `bot.py`, Matrix-моки, state, журналы |
| Модули `src/` | `test_config.py`, `test_utils.py`, … | Конфиг, utils, state, preferences, matrix_client |

---

## Systemd-сервис

### Установка

Отредактируйте файл `redmine-matrix-bot.service` — укажите правильные пути и имя пользователя:

```ini
[Unit]
Description=Redmine Matrix Notification Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your_user
Group=your_user
WorkingDirectory=/path/to/redmine-matrix-bot
ExecStart=/path/to/matrix_bot_firebeard/venv/bin/python3 bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Управление

```bash
sudo systemctl start redmine-matrix-bot     # Запуск
sudo systemctl stop redmine-matrix-bot      # Остановка
sudo systemctl restart redmine-matrix-bot   # Перезапуск
sudo systemctl status redmine-matrix-bot    # Статус
sudo systemctl enable redmine-matrix-bot    # Автозапуск при загрузке ОС
```

---

## Мониторинг

```bash
# Лог в реальном времени
tail -f bot.log

# Последние 50 строк
tail -50 bot.log

# Посмотреть state-файлы
python3 -m json.tool data/state_*_sent.json | head -30

# Проверка конфигурации
cd src && python3 -c "from config import validate_config; print(validate_config())"
```

### Пример лога (штатная работа)

```log
2025-07-14 16:39:40 [INFO] 🔍 Проверка в 16:39:40...
2025-07-14 16:39:47 [INFO] 👤 User 1972: 32 задач
2025-07-14 16:39:48 [INFO] 📨 #63603 → !room_id... (status_change)
2025-07-14 16:40:26 [INFO] 👤 User 3254: 29 задач
2025-07-14 16:40:45 [INFO] ✅ Проверка завершена
```

---

## Безопасность

| Аспект | Решение |
|--------|---------|
| Секреты | Хранятся в `.env`, не в коде |
| Авторизация Matrix | `access_token` (не пароль) |
| Git | `.env`, `data/`, `*.log` в `.gitignore` |
| Запись файлов | Атомарная (`tempfile` + `os.replace`) |
| Логи | Не содержат токенов и API-ключей |
| HTML-инъекции | `safe_html()` экранирует данные из Redmine |
| Systemd | Запуск от непривилегированного пользователя |
| Redmine API | Только чтение (`assigned_to_id=me`) |

---

## Решение проблем

### Бот не запускается

```bash
# Проверить конфигурацию
cd src && python3 -c "from config import validate_config; ok, m = validate_config(); print('OK' if ok else m)"

# Проверить зависимости
source venv/bin/activate
pip install -r requirements.txt

# Запустить вручную и посмотреть ошибки
python3 src/bot.py 2>&1 | head -30
```

### Бот не отправляет уведомления

1. Проверьте `MATRIX_ACCESS_TOKEN` — токен может быть просрочен
2. Проверьте, что бот присоединён к комнатам Matrix (пригласите его)
3. Проверьте `REDMINE_API_KEY` — ключ должен иметь доступ к задачам
4. Проверьте Redmine user ID в `config.yaml` — ID в URL профиля
5. Посмотрите лог: `tail -50 bot.log`

### Бот спамит старыми уведомлениями после перезапуска

Удалите state-файлы — бот заново проинициализирует состояние:

```bash
rm -f data/state_*.json
sudo systemctl restart redmine-matrix-bot
```

### Matrix: «не удалось отправить»

- В `bot.py` отправка повторяется до **3** раз с паузами **1 с** и **2 с** (экспоненциально).
- Проверьте, что бот-пользователь создан и приглашён в комнаты.
- Проверьте `MATRIX_HOMESERVER` — URL должен быть доступен с сервера бота.

### Тесты падают

```bash
# Убедитесь что запускаете из корня проекта
cd /path/to/redmine-matrix-bot
python -m pytest tests/ -v --tb=long
```

---

## Лицензия

MIT
