```markdown
# Via — Redmine → Matrix Notification Bot

Бот автоматически отслеживает изменения в задачах Redmine и отправляет уведомления в Matrix.

## Быстрый старт

```bash
git clone git@github.com:forgebeard/Via.git && cd Via
chmod +x deploy.sh && ./deploy.sh
```

**Что делает `deploy.sh`:**

1. Создаёт/дополняет `.env` при первом запуске.
2. Генерирует `POSTGRES_PASSWORD` и `APP_MASTER_KEY` (через init/openSSL fallback).
3. Запускает PostgreSQL, веб-панель и бота.

После запуска:

1. Откройте `http://<хост>:8080/setup` — создайте первого администратора.
2. Перейдите в **Настройки** (`/onboarding`) — введите параметры Matrix и Redmine, нажмите **«Проверить доступ»** → **«Сохранить»**.
3. Перезапустите бота: `docker compose restart bot`.
4. Заполните пользователей и группы в панели.

> ⚠️ Сохраните `.env` — в нём credentials для восстановления системы.

Подробности: [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md), [docs/ADMINISTRATOR_GUIDE.md](docs/ADMINISTRATOR_GUIDE.md), модель «панель — БД — бот»: [docs/ARCHITECTURE_ADMIN_DB_BOT.md](docs/ARCHITECTURE_ADMIN_DB_BOT.md), расширенный day zero: [docs/DAY_ZERO_EXTENDED.md](docs/DAY_ZERO_EXTENDED.md).

## Что умеет бот


| Функция             | Описание                                              |
| ------------------- | ----------------------------------------------------- |
| Новые задачи        | Уведомление при появлении назначенной задачи          |
| Смена статуса       | Уведомление при изменении статуса                     |
| Комментарии         | Отслеживание через Redmine journals API               |
| Просроченные задачи | Ежедневное уведомление                                |
| Напоминания         | По статусу «Информация предоставлена»                 |
| Маршрутизация       | Разные статусы/версии/команды → разные комнаты Matrix |
| Рабочие часы и DND  | Настраиваются через панель                            |


## Архитектура

```
┌──────────┐    REST API (polling)      ┌──────────────────┐  Matrix C-S API  ┌──────────┐
│  Redmine │◄───────────────────────────│  src/bot/main.py │────────────────►│  Matrix  │
│  (задачи)│    APScheduler: 90с        │  (entry point)   │  (уведомления)   │  (чат)   │
└──────────┘                            └──────┬───────────┘                   └──────────┘
                                               │
                        ┌──────────────────────┼──────────────────────┐
                        │                      │                      │
                   ┌────▼─────┐          ┌─────▼─────┐          ┌─────▼──────┐
                   │PostgreSQL│          │ Postgres  │          │  Postgres  │
                   │bot_users │          │bot_issue_ │          │ pending_   │
                   │groups    │          │ state     │          │ notifi-    │
                   │routes    │          │leases     │          │ cations    │
                   └──────────┘          └───────────┘          │  (DLQ)     │
                                                                └────────────┘

┌───────────────────────────────────────────────────────────────────────┐
│                        src/admin/ (FastAPI)                           │
│  /dashboard  /users  /groups  /settings  /events  /ops  /health       │
│  FastAPI + Jinja2 + HTMX → админ-панель на :8080                      │
└───────────────────────────────────────────────────────────────────────┘
```

**Ключевые принципы:**

- **Чистая бизнес-логика** (`bot/logic.py`) — без I/O, легко тестируется
- **Lease-координация** — несколько инстансов бота не дублируют работу
- **Dead-letter queue** — уведомления не теряются при сбое Matrix
- **Шифрование секретов** — AES-GCM, master key в Docker secret / env
- **Graceful shutdown** — корректная остановка по SIGTERM

Docker-сервисы: **bot** (опрос Redmine), **admin** (FastAPI панель), **postgres** (PostgreSQL 16), **docker-socket-proxy** (управление ботом из панели).

## Структура проекта

```
Via/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── deploy.sh
├── requirements.txt
├── src/
│   ├── bot/
│   │   ├── main.py              # Entry point: APScheduler, graceful shutdown
│   │   ├── logic.py             # Чистая бизнес-логика (без I/O)
│   │   ├── scheduler.py         # check_all_users, daily_report, DLQ retry
│   │   ├── sender.py            # Отправка через Jinja2 шаблон
│   │   └── heartbeat.py         # Heartbeat на админку
│   ├── admin/
│   │   ├── main.py              # FastAPI app, lifespan, routers
│   │   └── routes/              # 14 маршрутов (auth, users, groups, etc.)
│   ├── database/
│   │   ├── models.py            # 16 ORM-моделей (SQLAlchemy)
│   │   ├── state_repo.py        # bot_issue_state, bot_user_leases
│   │   └── dlq_repo.py          # Dead-letter queue для уведомлений
│   ├── config.py                # Загрузка .env, централизованные константы
│   ├── security.py              # Argon2, AES-GCM шифрование
│   └── matrix_send.py           # Отправка в Matrix с retry/backoff
├── templates/
│   ├── admin/                   # Панель администратора
│   └── bot/tpl_*.html.j2        # Именованные шаблоны Matrix / журнала
├── alembic/                     # Ревизии БД Alembic (в текущем дереве: initial schema)
├── tests/                       # 31 файл: pytest + Playwright E2E
└── docs/                        # ADMINISTRATOR_GUIDE, DEPLOYMENT, etc.
```

## Качество кода


| Инструмент     | Назначение                                                       |
| -------------- | ---------------------------------------------------------------- |
| **ruff**       | Линтер + форматтер (E, F, W, I, UP, S, SIM)                      |
| **mypy**       | Проверка типов (core files: logic, scheduler, routing, matrix_send, config, state_repo, load_config) |
| **pytest**     | 100+ юнит-тестов + интеграционные                                |
| **Playwright** | E2E тесты админ-панели                                           |
| **pre-commit** | Автоматическая проверка перед коммитом                           |

Политика CI: job `e2e` в PR работает в режиме non-blocking (`continue-on-error`) из-за
флакования браузерной инфраструктуры; блокирующим остаётся job `test`.


```bash
# Линтер
python -m ruff check src/

# Типы
PYTHONPATH=src python -m mypy src/bot/logic.py src/bot/scheduler.py \
  src/bot/routing.py src/matrix_send.py src/config.py src/database/state_repo.py \
  src/database/load_config.py \
  --explicit-package-bases

# Тесты
python -m pytest tests/ -v --tb=short --ignore=tests/e2e

# E2E (нужен браузер: python -m playwright install chromium)
python -m pytest tests/e2e/ -v --tb=short
```

## Конфигурация

Основные переменные в `.env` (см. [.env.example](.env.example)):


| Переменная                    | По умолчанию  | Описание                                                                         |
| ----------------------------- | ------------- | -------------------------------------------------------------------------------- |
| `PORTAL_BASE_URL`             | `REDMINE_URL` | Базовый URL портала для ссылок на задачу; если пусто, используется `REDMINE_URL` |
| `POLLING_INTERVAL_SEC`        | `90`          | Каноничный интервал опроса (сек)                                                 |
| `CHECK_INTERVAL`              | `90`          | Интервал опроса Redmine (сек)                                                    |
| `REMINDER_AFTER`              | `3600`        | Напоминание после (сек)                                                          |
| `GROUP_REPEAT_SECONDS`        | `1800`        | Повтор уведомлений в группу (сек)                                                |
| `DEDUP_TTL_HOURS`             | `24`          | TTL ключей дедупликации (часы)                                                   |
| `SUBJECT_MAX_LEN`             | `180`         | Максимальная длина темы задачи в карточке                                        |
| `MATRIX_RETRY_MAX_ATTEMPTS`   | `3`           | Попытки отправки в Matrix                                                        |
| `MATRIX_RETRY_BASE_DELAY_SEC` | `1.0`         | Базовая задержка retry (сек)                                                     |
| `BOT_LEASE_TTL_SECONDS`       | `300`         | Lease-координация (сек)                                                          |
| `HEARTBEAT_INTERVAL_SEC`      | `60`          | Heartbeat на админку (сек)                                                       |
| `CONFIG_POLL_INTERVAL_SEC`    | `30`          | Интервал повторной попытки, пока при старте не готовы секреты в БД (сек)         |
| `BOT_HOT_RELOAD`              | `1`           | Периодически подгружать конфиг из БД без рестарта (`0` — выкл.)                  |
| `BOT_HOT_RELOAD_INTERVAL_SEC` | `45`          | Интервал hot reload, секунды (15–3600)                                           |
| `CONTRACT_AUDIT_VERBOSE`      | `0`           | Подробные per-issue логи `journal_contract_check` (`1` — включить)               |
| `CONTRACT_AUDIT_SAMPLE_LIMIT` | `10`          | Лимит примеров issue_id в summary логе contract-check                            |


В onboarding задаётся только `REDMINE_URL`; `PORTAL_BASE_URL` синхронизируется автоматически с ним.

## Редактор шаблонов уведомлений

Во вкладке `Уведомления` используется единый **code-only** UX для всех шаблонов `tpl_`*:

- один редактор кода + live preview Matrix;
- автопредпросмотр с debounce (`~400ms`);
- `Сохранить` пишет `custom override` в БД (`notification_templates.body_html`);
- `Сбросить` удаляет override и возвращает файловый default из `templates/bot/tpl_*.html.j2`.

Block-editor endpoints удалены из runtime API. Контракт удаления: `404 Not Found` для
`/api/bot/notification-templates/compile-blocks`, `/{name}/decompose`,
`/{name}/decompose-body`, `/block-registry`.

## Перезапуск бота после изменений в панели

Бот при старте читает из БД секреты, пользователей, группы, маршруты и `cycle_settings`. Включён **hot reload** (`BOT_HOT_RELOAD=1`, по умолчанию): конфигурация из панели подтягивается периодически без рестарта. Если hot reload отключён или менялись только секреты в `.env` / ключевые интеграции, после правок **перезапустите бота**:

```bash
docker compose restart bot
```

Для systemd-схемы используйте имя юнита из вашего деплоя. Подробнее: [docs/ADMINISTRATOR_GUIDE.md](docs/ADMINISTRATOR_GUIDE.md) (раздел «Когда нужен перезапуск бота»).

## Документация


| Документ                                                                                             | Описание                                            |
| ---------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md)                                                 | Развёртывание на сервере (RHEL/AlmaLinux/Rocky)     |
| [docs/ADMINISTRATOR_GUIDE.md](docs/ADMINISTRATOR_GUIDE.md)                                           | Панель администратора, первый вход, troubleshooting |
| [docs/AUDIT_LOGGING.md](docs/AUDIT_LOGGING.md)                                                       | Логирование и аудит действий в панели               |
| [docs/MATRIX_NOTIFICATION_V5.md](docs/MATRIX_NOTIFICATION_V5.md)                                     | Формат карточки v5, дедупликация, txn_id, retry     |
| [docs/LOGGING_DUPLICATION_DIAGNOSIS_2026-04-21.md](docs/LOGGING_DUPLICATION_DIAGNOSIS_2026-04-21.md) | Диагностика и контроль дублей логов                 |
| [docs/secrets-storage.md](docs/secrets-storage.md)                                                   | Хранение секретов и шифрование                      |
| [docs/rollback-runbook.md](docs/rollback-runbook.md)                                                 | Аварийный откат                                     |
| [docs/ui-smoke-checklist.md](docs/ui-smoke-checklist.md)                                             | Smoke-чеклист UI                                    |


Конфигурация через `.env` — см. [.env.example](.env.example).

## Лицензия

MIT
```