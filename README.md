# 🤖 Redmine → Matrix Notification Bot

Бот автоматически отслеживает изменения в задачах Redmine и отправляет уведомления в Matrix-чат (Element / Synapse / любой Matrix-клиент).

### Актуальная схема

| Что | Как сейчас |
|-----|------------|
| Запуск | **`docker compose up --build -d`** или **`./deploy.sh`** |
| Код бота | `src/bot/main.py` (не корневой `bot.py`) |
| Админка | `src/admin/main.py` (FastAPI + Jinja2 + HTMX) |
| Пользователи и маршруты | Postgres таблицы (`bot_users` + routes) с редактированием через сервис **admin** |
| State | Postgres **`bot_issue_state`** (дедупликация и таймеры уведомлений) + lease по пользователю **`bot_user_leases`** |
| Интервал опроса Redmine | По умолчанию **90 с**; переопределение переменной **`CHECK_INTERVAL`** в `.env` (не ниже 15 с) |
| Matrix | Отправка через **`src/matrix_send.py`** (`room_send_with_retry`): до **3** попыток, паузы **1 с** и **2 с** |
| `src/preferences.py` (DND / рабочие часы) | **`can_notify()`** вызывается из **`send_safe`** и для утреннего отчёта: для личной комнаты — поля пользователя из Postgres; для **комнаты группы** — настройки группы (`group_delivery` в рантайме); приоритет «Аварийный» пробивает ограничения |

Ниже — рабочая документация по текущей схеме.

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
| 9 | **Рабочие часы и DND** | Применяются через `src/preferences.py` в `send_safe` и утреннем отчёте |

---

## Архитектура

### Логика бота (как сейчас)

```
┌──────────┐   REST API (~каждые 90с)   ┌──────────────────┐  Matrix C-S API   ┌──────────┐
│  Redmine │ ◄───────────────────────── │ src/bot/main.py  │ ────────────────► │  Matrix  │
│  (задачи)│                            │ APScheduler      │                   │  (чат)   │
└──────────┘                            └──────┬───────────┘                   └──────────┘
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
├── deploy.sh              # Zero-Config запуск
├── src/                   # Исходный код
│   ├── bot/
│   │   └── main.py        # Основной модуль бота
│   ├── admin/
│   │   └── main.py        # Веб-панель управления (FastAPI)
│   ├── database/
│   │   ├── models.py      # SQLAlchemy модели
│   │   └── session.py     # Сессии БД
│   ├── config.py          # Конфигурация, загрузка .env
│   ├── security.py        # Хеширование паролей, шифрование
│   ├── mail.py            # Отправка email
│   ├── matrix_client.py   # Matrix клиент
│   ├── matrix_send.py     # Отправка сообщений в Matrix
│   └── preferences.py     # Рабочие часы, DND, уведомления
├── alembic/               # Миграции БД
├── templates/             # Jinja2 шаблоны админки
├── scripts/               # Вспомогательные скрипты
└── tests/                 # pytest тесты
```

### Модули `src/` — краткое описание

| Модуль | Назначение |
|--------|-----------|
| `bot/main.py` | Основной цикл бота: опрос Redmine, отправка уведомлений в Matrix |
| `admin/main.py` | Веб-панель: аутентификация, настройки, управление пользователями |
| `database/models.py` | SQLAlchemy модели: BotUser, BotSession, SupportGroup и др. |
| `database/session.py` | Async-сессии БД, формирование DATABASE_URL |
| `config.py` | Загрузка `.env`, пути, статусы Redmine, `validate_config()` |
| `security.py` | Хеширование паролей (argon2), шифрование секретов (AES-GCM) |
| `mail.py` | Отправка email, маска идентификатора |
| `matrix_client.py` | Singleton AsyncClient, access_token |
| `matrix_send.py` | Отправка в Matrix с retry |
| `preferences.py` | Рабочие часы, DND, `can_notify()` с Emergency bypass |

---

## Быстрый старт

### 1. Клонирование и окружение

```bash
git clone git@github.com:forgebeard/Via.git
cd Via

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Docker-развёртывание (Zero-Config)

**На новой ВМ — без редактирования файлов!**

```bash
# Просто запускаем — .env создастся автоматически
docker compose up --build -d

# Смотрим логи init-сервиса (генерация credentials)
docker compose logs init

# Credentials сохранены в .env — их можно посмотреть в GUI админки
```

**Что происходит при первом запуске:**
1. Сервис `init` генерирует случайные `POSTGRES_PASSWORD` и `APP_MASTER_KEY`
2. Сохраняет их в файл `.env` в корне проекта
3. PostgreSQL стартует с этими credentials
4. Админка и бот подключаются к БД

**После запуска:**
1. Откройте `http://<хост>:8080/setup` — создайте первого администратора
2. Войдите в админку: `http://<хост>:8080/login`
3. Перейдите в **Настройки** (`/onboarding`) — там раздел **"База данных сервиса"**
4. Введите параметры Matrix и Redmine в разделе **"Параметры сервиса"**
5. Сохраните — бот автоматически подхватит настройки

> ⚠️ **Важно:** Сохраните credentials из `.env` или из GUI в надёжном месте!
> Если потеряете `APP_MASTER_KEY` — не сможете расшифровать секреты в БД.

### 3. Настройка пользователей и маршрутизации

Пользователи и маршрутизация хранятся в Postgres. Заполните таблицы через admin UI: `bot_users` (пользователи) + `support_groups` (ID комнаты группы, привязка статусов Redmine к этой комнате, типы уведомлений и рабочие часы для дублей в неё) + `status_room_routes` / `version_room_routes` (доп. комнаты).

### 4. Проверка конфигурации (модуль `src/`)

```bash
cd src && python3 -c "from config import validate_required_env; ok, m = validate_required_env(); print('OK' if ok else m)"
```

### 5. Запуск тестов

Зависимости для pytest: `pip install -r requirements.txt -r requirements-test.txt` (в `requirements-test.txt` есть **httpx** для `TestClient` админки).

Если задан **`DATABASE_URL`** на Postgres, он должен совпадать с реальным сервером (пароль/пользователь из `.env` или из `docker compose`). Пример из CI (`postgresql://bot:postgres@localhost:5432/via`) подходит только при таком же контейнере или настройке `pg_hba`.

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
| **init** | Генерирует `.env` со случайными `POSTGRES_PASSWORD` и `APP_MASTER_KEY` при первом запуске |
| **bot** | Образ из `Dockerfile` (корневой `bot.py` + `src/`), том `./data` → `/app/data`, `.env` только для чтения; healthcheck: `python -c "import bot"` |
| **postgres** | PostgreSQL 16, том `postgres_data`; `DATABASE_URL` в **bot** и **admin** |
| **docker-socket-proxy** | Ограниченный прокси к Docker API для runtime-control из admin (без прямого монтирования raw socket в admin). Если Start/Stop не срабатывают, на дашборде в уведомлении показывается текст ошибки Docker; см. комментарии к переменным `DOCKER_*` в `.env.example` (в том числе `DOCKER_TARGET_CONTAINER_SUBSTRING`). |
| **admin** | Панель (`admin_main.py`, FastAPI + Jinja2 + HTMX): **дашборд** (`/dashboard`, тот же экран на `/`), **группы** (в т.ч. маршруты по статусу Redmine в комнату группы), **пользователи**, **настройки** (`/onboarding`), **события** (`/events`: таблица и CSV по файлу **`ADMIN_EVENTS_LOG_PATH`** / `data/bot.log`, фильтр по датам, **`ADMIN_EVENTS_LOG_SCAN_BYTES`**, **`ADMIN_EVENTS_LOG_PARSE_AS_UTC`**, строки **`[ADMIN]`**; аудит панели — отдельный **`ADMIN_AUDIT_LOG_PATH`** / `data/admin_audit.log`); маршруты по версии — URL **`/routes/version`** (без пункта в меню); runtime-control бота; **CSP** (`ADMIN_ENABLE_CSP` / `ADMIN_CSP_POLICY`); **`ADMIN_PORT`** (8080); при старте `alembic upgrade head`; том **`./data`** у сервиса **с записью** (чтобы дописывать логи и читать `runtime_status.json`); **управление credentials БД** через GUI |

### Подготовка `.env`

**НЕ ТРЕБУЕТСЯ!** При первом запуске `docker compose up --build -d` сервис `init` автоматически создаст `.env` со случайными:
- `POSTGRES_PASSWORD` — пароль для PostgreSQL
- `APP_MASTER_KEY` — мастер-ключ для шифрования секретов в БД

Если нужно переопределить значения по умолчанию — создайте `.env` вручную:
```env
POSTGRES_USER=bot
POSTGRES_DB=via
ADMIN_PORT=8080
BOT_TIMEZONE=Europe/Moscow
```

> ⚠️ **Важно:** Сохраните сгенерированные credentials в надёжном месте!
> Их можно посмотреть:
> - В файле `.env` в корне проекта
> - В GUI админки: Настройки → **База данных сервиса**

### Сборка и запуск

Из корня репозитория (где лежат `Dockerfile` и `docker-compose.yml`):

```bash
# Создать каталог data при необходимости (том смонтируется пустым)
mkdir -p data

# Сборка образа и запуск в фоне (.env создастся автоматически)
docker compose up --build -d

# Смотрим логи init-сервиса (генерация credentials)
docker compose logs init

# Логи бота
docker compose logs -f bot

# Остановка
docker compose down
```

Данные **Postgres** сохраняются в томе `postgres_data` (конфиг + `bot_issue_state`/lease). State больше не пишется в JSON.

### Админка и конфиг в БД

**Пошаговое руководство для администраторов** (развёртывание, первый вход, пароли, порты, локальный `DATABASE_URL`): [docs/ADMINISTRATOR_GUIDE.md](docs/ADMINISTRATOR_GUIDE.md).

Что попадает в раздел **События** (файл лога), отдельный **файл аудита** `[AUDIT]`, stdout и опционально БД: [docs/ADMIN_EVENTS_AND_AUDIT_PLAN.md](docs/ADMIN_EVENTS_AND_AUDIT_PLAN.md).


1. После `docker compose up` откройте `http://<хост>:8080/setup` и создайте первого admin (только если admin ещё нет в БД).
2. Вход в админку: `http://<хост>:8080/login` по логину и паролю.
3. Сброс пароля админки: другой администратор (**Аккаунты панели**) или скрипт `scripts/reset_admin_password.py` при доступе к БД; самообслуживания по ссылке «забыли пароль» нет.
4. Перейдите в **Настройки** (`/onboarding`) → раздел **"База данных сервиса"** — здесь можно посмотреть текущие credentials и перегенерировать их при необходимости.
5. Введите параметры Matrix и Redmine в разделе **"Параметры сервиса"** → нажмите **"Сохранить"**.
6. Заполните пользователей и **группы** в админке (маршруты по **статусу** Redmine задаются в карточке группы — комната группы и список статусов; маршруты по **версии** — отдельная таблица, страница **`/routes/version`** при необходимости). Раздел **События** строится по файлу лога (**`ADMIN_EVENTS_LOG_PATH`**, иначе `data/bot.log`); журнал аудита панели — **`ADMIN_AUDIT_LOG_PATH`** (см. [ADMINISTRATOR_GUIDE.md](docs/ADMINISTRATOR_GUIDE.md)). После изменений в БД перезапустите сервис **`bot`** (он перечитывает конфиг при старте).
7. Для дедупликации на нескольких инстансах используется lease по пользователю (`bot_user_leases`) и state в `bot_issue_state`.

Миграции схемы БД выполняются при старте сервиса **admin**: `alembic upgrade head`.

### Перегенерация credentials

Если нужно сменить пароль БД или мастер-ключ:

**Через GUI (рекомендуется):**
1. Настройки → **База данных сервиса** → кнопка **"Сгенерировать новые"**
2. Подтвердите действие
3. Перезапустите контейнеры: `docker compose restart postgres bot admin`

**Вручную (через init-скрипт):**
```bash
# Перегенерация .env с новыми credentials
docker compose run --rm -e REGENERATE=1 init
# Перезапуск контейнеров
docker compose restart postgres bot admin
```

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
# MATRIX_DEVICE_ID необязателен — по умолчанию используется redmine_bot

# ─── Redmine ─────────────────────────────────────────
REDMINE_URL=https://redmine.example.com
REDMINE_API_KEY=your_redmine_api_key
```

### Где взять токены

| Параметр | Где получить |
|----------|-------------|
| `MATRIX_ACCESS_TOKEN` | Element → Настройки → Помощь и О программе → Access Token |
| `MATRIX_USER_ID` | Формат: `@username:your.server` |
| `MATRIX_DEVICE_ID` | Необязателен — по умолчанию `redmine_bot` |
| `REDMINE_API_KEY` | Redmine → Моя учётная запись → API-ключ (правая колонка) |

> ⚠️ **Важно:** Бот использует `access_token` (не пароль) для авторизации в Matrix — это безопаснее и не требует login-flow.

> ⚠️ **Важно:** API-ключ Redmine определяет, чьи задачи бот может видеть. Рекомендуется использовать ключ с правами администратора, если бот обслуживает нескольких пользователей.

### Логи (опционально)

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `LOG_TO_FILE` | `1` | `0`, `false`, `no`, `off` — не открывать файл, только stdout (удобно вместе с `docker logs`). |
| `LOG_PATH` | *(пусто)* | Файл лога: не задано → `data/bot.log`; иначе путь **от корня репозитория** или абсолютный. |
| `LOG_MAX_BYTES` | `5242880` | Ротация: при достижении размера файл переименовывается в `bot.log.1` и создаётся новый (5 МБ по умолчанию). |
| `LOG_BACKUP_COUNT` | `5` | Сколько архивных файлов хранить (`bot.log.1` …); минимум **1**. Итого на диске ≈ `(LOG_BACKUP_COUNT + 1) × LOG_MAX_BYTES` в худшем случае. |
| `CHECK_INTERVAL` | `90` | Интервал опроса Redmine в секундах (15–86400). Если цикл дольше интервала, в лог пишется предупреждение — для SLA «до нескольких минут» это нормально. |

При достижении **`LOG_MAX_BYTES`** текущий `bot.log` переименовывается в `bot.log.1`, старые `bot.log.N` сдвигаются по номеру, создаётся новый пустой `bot.log`; лишние архивы сверх **`LOG_BACKUP_COUNT`** удаляются (стандартное поведение `RotatingFileHandler` в Python). Подробнее для администраторов: [docs/ADMINISTRATOR_GUIDE.md](docs/ADMINISTRATOR_GUIDE.md) (раздел про панель и события).

### Admin auth / security

На экране панели даты и время (аккаунты, секреты, сервис, события после форматирования) показываются как **ДД.ММ.ГГГ ЧЧ:ММ:СС** в **`BOT_TIMEZONE`**, без микросекунд и без суффикса часового пояса в строке.

| Переменная | Назначение |
|------------|------------|
| `ADMIN_LOGINS` | Список разрешённых логинов панели (через запятую); пусто = без ограничения |
| `ADMIN_EVENTS_LOG_PATH` | Файл лога для таблицы и CSV на `/events`; пусто → `data/bot.log` от корня приложения админки |
| `ADMIN_EVENTS_LOG_SCAN_BYTES` | Сколько байт читать с конца файла (если файл больше — хвост); по умолчанию 8 МиБ |
| `ADMIN_AUDIT_LOG_PATH` | Отдельный файл строк `[AUDIT]` (Docker + CRUD); пусто → `data/admin_audit.log`; `-` / `none` — не писать |
| `ADMIN_EVENTS_LOG_PARSE_AS_UTC` | `1` (по умолчанию): префикс `YYYY-MM-DD` в файле считать UTC и показывать в `BOT_TIMEZONE`; `0` — время в файле уже в `BOT_TIMEZONE` |
| `ADMIN_EVENTS_LOG_CRUD` | `1` / `true` / `yes` / `on` — дописывать в тот же файл строки `CRUD …` (пользователи, группы, маршруты, «Мои настройки»); по умолчанию выкл. |
| `ADMIN_AUDIT_CRUD_DB` | Не задано — дублировать CRUD в **`bot_ops_audit`** (как при включённом файле); `0` — не писать в БД; `1` — писать в БД (можно без `ADMIN_EVENTS_LOG_CRUD`). |
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
cd /path/to/Via
python -m pytest tests/ -v --tb=long
```

---

## Лицензия

MIT
