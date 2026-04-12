# Via — Redmine → Matrix Notification Bot

Бот автоматически отслеживает изменения в задачах Redmine и отправляет уведомления в Matrix.

## Быстрый старт

```bash
git clone git@github.com:forgebeard/Via.git && cd Via
docker compose up --build -d
```

После запуска:
1. Откройте `http://<хост>:8080/setup` — создайте первого администратора
2. Перейдите в **Настройки** (`/onboarding`) и введите параметры Matrix и Redmine
3. Заполните пользователей и группы в панели

> ⚠️ Сохраните `.env` — в нём credentials для восстановления системы.

Подробности: [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md), [docs/ADMINISTRATOR_GUIDE.md](docs/ADMINISTRATOR_GUIDE.md).

## Что умеет бот

| Функция | Описание |
|---------|----------|
| Новые задачи | Уведомление при появлении назначенной задачи |
| Смена статуса | Уведомление при изменении статуса |
| Комментарии | Отслеживание через Redmine journals API |
| Просроченные задачи | Ежедневное уведомление |
| Напоминания | По статусу «Информация предоставлена» |
| Маршрутизация | Разные статусы/версии/команды → разные комнаты Matrix |
| Рабочие часы и DND | Настраиваются через панель |

## Архитектура

```
┌──────────   REST API (~каждые 90с)   ┌──────────────────┐  Matrix C-S API   ┌──────────┐
│  Redmine │ ◄───────────────────────── │ src/bot/main.py  │ ────────────────► │  Matrix  │
│  (задачи)│                            │ APScheduler      │                   │  (чат)   │
└──────────┘                            └──────┬───────────┘                   └──────────┘
                                               │
                                        ┌──────▼─────────────────────┐
                                        │ Postgres: bot_issue_state  │
                                        │ + bot_user_leases (lease)  │
                                        └────────────────────────────┘
```

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
│   ├── bot/main.py          # Основной цикл бота
│   ├── admin/main.py        # Веб-панель (FastAPI + Jinja2 + HTMX)
│   ├── admin/routes/        # Маршруты админки
│   ├── database/            # SQLAlchemy модели и сессии
│   ├── config.py            # Загрузка .env
│   ├── security.py          # Хеширование, шифрование
│   ├── matrix_send.py       # Отправка в Matrix с retry
│   └── preferences.py       # Рабочие часы, DND
├── templates/admin/
│   ├── auth/                # login, setup, reset_password, onboarding
│   └── panel/               # dashboard, users, groups, events, settings
├── static/admin/css/        # Стили панели
├── alembic/                 # Миграции БД
├── scripts/                 # Вспомогательные скрипты
└── tests/                   # pytest + Playwright E2E
```

## Тесты

```bash
# Юнит-тесты и API
python -m pytest tests/ -v --tb=short --ignore=tests/e2e

# E2E (нужен браузер: python -m playwright install chromium)
python -m pytest tests/e2e/ -v --tb=short
```

## Документация

| Документ | Описание |
|----------|----------|
| [docs/DEPLOYMENT_GUIDE.md](docs/DEPLOYMENT_GUIDE.md) | Развёртывание на сервере (RHEL/AlmaLinux/Rocky) |
| [docs/ADMINISTRATOR_GUIDE.md](docs/ADMINISTRATOR_GUIDE.md) | Панель администратора, первый вход, troubleshooting |
| [docs/AUDIT_LOGGING.md](docs/AUDIT_LOGGING.md) | Логирование и аудит действий в панели |
| [docs/secrets-storage.md](docs/secrets-storage.md) | Хранение секретов и шифрование |
| [docs/rollback-runbook.md](docs/rollback-runbook.md) | Аварийный откат |
| [docs/ui-smoke-checklist.md](docs/ui-smoke-checklist.md) | Smoke-чеклист UI |

Конфигурация через `.env` — см. [.env.example](.env.example).

## Лицензия

MIT
