# 🤖 Redmine → Matrix Notification Bot

Бот автоматически отслеживает изменения в задачах Redmine и отправляет уведомления в Matrix-чат (Element / Synapse / любой Matrix-клиент).

### Актуальная схема (репозиторий `matrix_bot_firebeard`)

| Что | Как сейчас |
|-----|------------|
| Запуск | **`python bot.py`** из корня репозитория (файл **`bot.py`**, не `src/bot.py`) |
| Пользователи и маршруты | Postgres таблицы (`bot_users` + routes) с редактированием через сервис **admin** |
| State | Postgres **`bot_issue_state`** (дедупликация и таймеры уведомлений) + lease по пользователю **`bot_user_leases`** |
| Интервал опроса Redmine | По умолчанию **90 с**; переопределение переменной **`CHECK_INTERVAL`** в `.env` (не ниже 15 с) |
| Matrix | Отправка через **`src/matrix_send.py`** (`room_send_with_retry`): до **3** попыток, паузы **1 с** и **2 с** |
| PostgreSQL (Docker) | Сервис в **`docker-compose.yml`**; конфиг пользователей/маршрутов и **state** — в Postgres через **админку** |
| `src/preferences.py` (DND / рабочие часы) | **`can_notify()`** вызывается из **`send_safe`** и для утреннего отчёта: поля в объекте пользователя в памяти (загружаются из Postgres); приоритет «Аварийный» пробивает ограничения |

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

### Логика бота (как сейчас)

```
┌──────────┐   REST API (~каждые 90с)   ┌──────────────┐  Matrix C-S API   ┌──────────┐
│  Redmine │ ◄───────────────────────── │   bot.py     │ ────────────────► │  Matrix  │
│  (задачи)│                            │ APScheduler  │                   │  (чат)   │
└──────────┘                            └──────┬───────┘                   └──────────┘
                                               │
                                        ┌──────▼─────────────────────┐
                                        │ Postgres: bot_issue_state  │
                                        │ + bot_user_leases (lease)  │
                                        └────────────────────────────┘
```

### Docker / Production (admin + state в БД)

```
┌──────────┐                     ┌─────────────┐
│  bot     │ ─── DATABASE_URL ─► │ PostgreSQL  │
│  (контейнер)                  │  (volume)   │    bot_issue_state + lease
└──────────┘                     └─────────────┘
       │ (только данные/логи)
```

**Цикл работы:**
1. Планировщик вызывает проверку каждые `CHECK_INTERVAL` секунд (по умолчанию 90).
2. Для каждого пользователя загружаются открытые назначенные задачи из Redmine.
3. Текущее состояние сравнивается с сохранённым в **Postgres** (`bot_issue_state`).
4. При обнаружении изменений — HTML-уведомление собирается и отправляется в Matrix (с повторами при сбое).
5. Новое состояние upsert-ится в Postgres (и/или lease ограничивает дубль отправок).

---

## Стек технологий

| Компонент | Технология | Назначение |
|-----------|-----------|------------|
| Язык | Python 3.11+ | Основной язык |
| Matrix API | `matrix-nio` | Async-отправка HTML-сообщений в чат |
| Redmine API | `python-redmine` | Получение задач, журналов, статусов |
| Планировщик | `APScheduler` | Периодические проверки |
| Конфигурация | `python-dotenv` | Секреты из `.env` |
| Тестирование | `pytest` + `pytest-asyncio` | ~190+ тестов (`tests/`) |
| Прод-запуск | Docker Compose | Том `./data`, логи: `docker compose logs -f bot` |
| Контейнеризация | Dockerfile + Compose | См. раздел «Docker / Production запуск» |
| БД (резерв) | PostgreSQL 16 | В compose; приложение пока не использует |

---

## Структура проекта

```
matrix_bot_firebeard/
├── bot.py                 # Точка входа: Redmine + Matrix + APScheduler
├── Dockerfile             # Многоступенчатая сборка образа бота (Python 3.11)
├── docker-compose.yml     # Сервисы: bot + postgres + тома
├── .dockerignore          # Исключения из контекста docker build
├── data/
│   ├── bot.log            # Лог (ротация в коде)
├── requirements.txt
├── requirements-test.txt
├── requirements-lock.txt
├── README.md
├── .env                   # Секреты — не коммитить
├── .cursorrules           # Правила для ассистента в Cursor
├── src/                   # Общие модули и задел под рефакторинг
│   ├── config.py
│   ├── utils.py           # в т.ч. safe_html()
│   ├── matrix_client.py
│   ├── matrix_send.py
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
| `preferences.py` | Рабочие часы, DND, `can_notify()` с Emergency bypass | 27 |
| `matrix_client.py` | Singleton `AsyncClient`, access_token, `send_message()` → `matrix_send` | 10 |
| `matrix_send.py` | Общая отправка в Matrix с retry (использует и `bot.py`, и `matrix_client`) | — |
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
./scripts/generate_master_key.sh master_key.txt
nano .env   # заполнить все переменные (см. раздел «Настройка .env»)
```

`master_key.txt` — локальный dev-вариант хранения ключа. Для production используйте secret manager (Docker/K8s secret) или защищённый канал передачи `APP_MASTER_KEY_FILE`.

Подробности по хранению секретов: `docs/secrets-storage.md`.

### 3. Настройка пользователей и маршрутизации

Пользователи и маршрутизация хранятся в Postgres. Заполните таблицы через admin UI: `bot_users` (пользователи) + `status_room_routes` / `version_room_routes` (доп. комнаты).

### 4. Проверка конфигурации (модуль `src/`)

```bash
cd src && python3 -c "from config import validate_required_env; ok, m = validate_required_env(); print('OK' if ok else m)"
```

### 5. Запуск тестов

Зависимости для pytest: `pip install -r requirements.txt -r requirements-test.txt` (в `requirements-test.txt` есть **httpx** для `TestClient` админки).

Если задан **`DATABASE_URL`** на Postgres, он должен совпадать с реальным сервером (пароль/пользователь из `.env` или из `docker compose`). Пример из CI (`postgresql://bot:postgres@localhost:5432/redmine_matrix`) подходит только при таком же контейнере или настройке `pg_hba`.

```bash
# Юнит- и API-тесты (без браузера)
python -m pytest tests/ -v --tb=short --ignore=tests/e2e
```

**E2E (Playwright)** — `tests/e2e/`: поднимают отдельный `uvicorn` и Chromium. Нужны `DATABASE_URL` на Postgres, после установки браузера: `python -m playwright install chromium`. Полный сценарий входа выполняется либо при пустой БД (одноразовая регистрация через `/setup` в фикстуре), либо при заданных **`E2E_ADMIN_LOGIN`** и **`E2E_ADMIN_PASSWORD`** в окружении.

```bash
python -m pytest tests/e2e/ -v --tb=short
```

В CI E2E вынесены в отдельный job (см. `.github/workflows/ci.yml`).

### 6. Тестовый запуск бота

```bash
python3 bot.py
```

Если в логе видно `✅ Matrix: ...` и `✅ Redmine: ...` — бот работает. Остановить — `Ctrl+C`.

Постоянный запуск на сервере — через **Docker Compose** (следующий раздел).

---

## Docker / Production запуск

### Что входит в compose

| Сервис | Назначение |
|--------|------------|
| **bot** | Образ из `Dockerfile` (корневой `bot.py` + `src/`), том `./data` → `/app/data`, `.env` только для чтения; healthcheck: `python -c "import bot"` |
| **postgres** | PostgreSQL 16, том `postgres_data`; `DATABASE_URL` в **bot** и **admin** |
| **docker-socket-proxy** | Ограниченный прокси к Docker API для runtime-control из admin (без прямого монтирования raw socket в admin) |
| **admin** | Веб-интерфейс (`admin_main.py`, FastAPI + Jinja2 + HTMX): шаблоны в `templates/admin/`, стили в `static/admin/css/` (раздача `/static/...`); ссылки на CSS с `?v=…` из **`ADMIN_ASSET_VERSION`** (по умолчанию `1`) для сброса кэша после обновления стилей; runtime-control (`start/stop/restart` сервиса `bot`) через `DOCKER_HOST` + `DOCKER_TARGET_SERVICE`; опционально **CSP** через `ADMIN_ENABLE_CSP` / `ADMIN_CSP_POLICY` в `.env`. Порт: **`ADMIN_PORT`** (по умолчанию 8080); при старте `alembic upgrade head` |

### Подготовка `.env`

Сначала добавьте переменные для PostgreSQL (используются и `docker compose`, и подстановка `DATABASE_URL` в контейнере бота):

```env
POSTGRES_USER=bot
POSTGRES_PASSWORD=сгенерируйте_надёжный_пароль
POSTGRES_DB=redmine_matrix
```

Остальные переменные — как в разделе «Настройка .env» ниже (`MATRIX_*`, `REDMINE_*` и параметры admin).

> ⚠️ Если в пароле есть символы `@ : / ? #` — для `DATABASE_URL` может понадобиться URL-кодирование или упрощённый пароль для dev.

### Сборка и запуск

Из корня репозитория (где лежат `Dockerfile` и `docker-compose.yml`):

```bash
# Создать каталог data при необходимости (том смонтируется пустым)
mkdir -p data

# Сборка образа и запуск в фоне
docker compose up --build -d

# Логи бота
docker compose logs -f bot

# Остановка
docker compose down
```

Данные **Postgres** сохраняются в томе `postgres_data` (конфиг + `bot_issue_state`/lease). State больше не пишется в JSON.

### Админка и конфиг в БД

**Пошаговое руководство для администраторов** (развёртывание, первый вход, пароли, порты, локальный `DATABASE_URL`): [docs/ADMINISTRATOR_GUIDE.md](docs/ADMINISTRATOR_GUIDE.md).

1. После `docker compose up` откройте `http://<хост>:8080/setup` и создайте первого admin (только если admin ещё нет в БД).
2. Вход в админку: `http://<хост>:8080/login` по логину и паролю.
3. Восстановление пароля: `http://<хост>:8080/forgot-password` → одноразовый reset token.
4. Заполните пользователей, маршруты и секреты в админке; затем перезапустите сервис **`bot`** (бот читает конфиг при старте).
5. Для дедупликации на нескольких инстансах используется lease по пользователю (`bot_user_leases`) и state в `bot_issue_state`.

Миграции схемы БД выполняются при старте сервиса **admin**: `alembic upgrade head`.

### Только пересборка образа бота

```bash
docker compose build bot
docker compose up -d bot
```

### Права на каталог `data/`

Том `./data` на хосте должен быть **доступен на запись** пользователю процесса в контейнере (по умолчанию uid **1000** / пользователь `bot` в образе). Если каталог создал root или другой uid, возможны `PermissionError` при записи лога — выставьте владельца, например: `sudo chown -R 1000:1000 data` (подставьте нужный uid), либо см. [user namespace](https://docs.docker.com/engine/security/userns-remap/) в документации Docker. При отказе записи в файл лога бот продолжит работу и писать только в stdout (`docker compose logs`).

<!-- Убрано упоминание systemd: теперь только Docker Compose -->

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

### Логи (опционально)

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `LOG_TO_FILE` | `1` | `0`, `false`, `no`, `off` — не открывать файл, только stdout (удобно вместе с `docker logs`). |
| `LOG_PATH` | *(пусто)* | Файл лога: не задано → `data/bot.log`; иначе путь **от корня репозитория** или абсолютный. |
| `CHECK_INTERVAL` | `90` | Интервал опроса Redmine в секундах (15–86400). Если цикл дольше интервала, в лог пишется предупреждение — для SLA «до нескольких минут» это нормально. |

### Admin auth / security

| Переменная | Назначение |
|------------|------------|
| `ADMIN_LOGINS` | Список разрешённых логинов панели (через запятую); пусто = без ограничения |
| `AUTH_TOKEN_SALT` | Соль для hash reset-токенов |
| `SESSION_TTL_SECONDS` | Время жизни admin-сессии |
| `RESET_TOKEN_TTL_SECONDS` | TTL токена сброса пароля |
| `RESET_COOLDOWN_SECONDS` | Ограничение частоты reset-запросов |
| `COOKIE_SECURE` | Secure-флаг cookie (`1` для HTTPS) |
| `APP_MASTER_KEY_FILE` | Путь к master key (32 байта) |
| `SHOW_DEV_TOKENS` | Показ dev reset-токена в UI (только dev/test) |

### Health endpoints

- `GET /health/live` — процесс поднят.
- `GET /health/ready` — доступны БД и master key.

### Recovery

Аварийный сброс пароля администратора:

```bash
python scripts/reset_admin_password.py --login admin --password 'NewStrongPassword123'
```

Полный регламент отката: `docs/rollback-runbook.md`.
Smoke-чеклист UI перед merge: `docs/ui-smoke-checklist.md`.
План глобального редизайна админ-панели: `docs/ui-global-overhaul-plan.md`.

### Расписание и DND (из Postgres)

| Поле | Пример | Описание |
|------|--------|----------|
| `work_hours` | `"09:00-18:00"` | Окно, вне которого уведомления не шлются (кроме приоритета «Аварийный»). |
| `work_days` | `[0,1,2,3,4]` | Дни недели 0=Пн … 6=Вс; по умолчанию пн–пт. |
| `dnd` | `true` | Вручную отключить все уведомления пользователю. |

---

## Настройка пользователей (YAML-пример для справки)

> Настройки пользователей и маршрутизации читаются из Postgres (таблицы `bot_users` и routes) и редактируются через admin UI.

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

## State в Postgres

Состояние дедупликации и таймеры уведомлений хранится в Postgres:

- `bot_user_leases` — lease по `user_redmine_id`, чтобы 3–5 параллельных инстансов бота не отправляли дубликаты.
- `bot_issue_state` — по `(user_redmine_id, issue_id)` хранит:
  - `last_status` и `sent_notified_at`
  - `last_journal_id` (детект новых журналов)
  - `last_reminder_at` и `last_overdue_notified_at`

> 💡 При первом запуске бот НЕ шлёт уведомления для задач — он заполняет состояние (baseline), чтобы не устроить спам после деплоя.

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

В проекте **сотни тестов** в `tests/` (корневой `test_bot.py` и модули `src/`). В CI (`.github/workflows/ci.yml`) на push и pull request: **pytest** без каталога `tests/e2e/`, отдельный job **Playwright E2E** по `tests/e2e/`, **сборка Docker-образа** с проверкой `python -c "import bot"`.

```bash
# Юнит-тесты и API (без Playwright)
python -m pytest tests/ -v --tb=short --ignore=tests/e2e

# Один модуль
python -m pytest tests/test_preferences.py -v

# С покрытием (если установлен pytest-cov)
python -m pytest tests/ --cov=src --cov-report=term-missing
```

| Область | Файлы | Что проверяется |
|--------|-------|-----------------|
| Корневой бот | `test_bot.py` | Логика `bot.py`, Matrix-моки, state, журналы |
| Модули `src/` | `test_config.py`, `test_utils.py`, … | Конфиг, utils, state, preferences, matrix_client |
| Админка E2E | `tests/e2e/` | Playwright: страница логина, редирект без сессии, вход (при пустой БД или `E2E_ADMIN_*`) |

---

## Мониторинг

```bash
# Лог контейнера (Docker)
docker compose logs -f bot

# Лог в файл на хосте (если пишется в data/bot.log)
tail -f data/bot.log

# Последние 50 строк файла
tail -50 data/bot.log

# Проверить состояние в Postgres
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select count(*) from bot_issue_state;"

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
| Docker | В образе процесс под непривилегированным пользователем (`USER bot`) |
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
python3 bot.py 2>&1 | head -30
```

### Бот не отправляет уведомления

1. Проверьте `MATRIX_ACCESS_TOKEN` — токен может быть просрочен
2. Проверьте, что бот присоединён к комнатам Matrix (пригласите его)
3. Проверьте `REDMINE_API_KEY` — ключ должен иметь доступ к задачам
4. Проверьте Redmine user ID в `config.yaml` — ID в URL профиля
5. Посмотрите лог: `tail -50 data/bot.log`

### Бот спамит старыми уведомлениями после перезапуска

Очистите state в Postgres — бот заново проинициализирует дедупликацию:

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "delete from bot_issue_state;"
docker compose restart bot
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
