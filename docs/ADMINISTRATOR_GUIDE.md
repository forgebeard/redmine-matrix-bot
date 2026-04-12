# Руководство администратора

> Для развёртывания на сервере см. [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md).
> Для обзора проекта см. [README.md](../README.md).

## 1. Первый вход

### 1.1. Одноразовая регистрация (`/setup`)

Страница доступна, пока в БД нет ни одного администратора:

`http://<хост>:8080/setup`

Задайте логин и пароль. После этого используйте `/login`.

> Если `/setup` говорит что админ уже есть, а том `postgres_data` использовался ранее — запись могла остаться с прошлых запусков. Варианты: войти под существующим логином, сбросить пароль скриптом `scripts/reset_admin_password.py`, либо для чистой среды: `docker compose down -v` (данные БД удалятся).

### 1.2. Ограничение по логину

Переменная `ADMIN_LOGINS` в `.env` — список разрешённых логинов (через запятую). Пустое = без ограничения.

## 2. Настройка интеграций

После входа перейдите в **Настройки** (`/onboarding`):

1. **Параметры сервиса** — введите URL и API-ключ Redmine, параметры Matrix (Homeserver, Access Token, User ID). Нажмите **«Проверить доступ»**, затем **«Сохранить»**.
2. **База данных сервиса** — скопируйте `POSTGRES_PASSWORD` и `APP_MASTER_KEY` в безопасное место.
3. Перезапустите бота: `docker compose restart bot`.

## 3. Панель: разделы

| Раздел | URL | Описание |
|--------|-----|----------|
| Дашборд | `/dashboard` | Плитки Пользователи / Группы / События, блок «Что сделать сейчас», управление ботом (Старт/Стоп/Рестарт) |
| Пользователи | `/users` | Список, создание, редактирование (Redmine ID, Matrix user, настройки) |
| Группы | `/groups` | Комната Matrix группы, статусы Redmine, версии |
| Настройки | `/onboarding` | Параметры сервиса, БД, таймзона, справочник |
| Аккаунты панели | `/app-users` | Управление аккаунтами админки (смена логина, сброс пароля) |
| События | `/events` | Таблица по файлу лога + CSV-экспорт |
| Маршруты по версии | `/routes/version` | Глобальная карта версия → комната |

После правок в панели **перезапустите bot** — он подгружает конфигурацию из Postgres при старте.

## 4. Смена пароля админки

Самообслуживания по форме «забыли пароль» нет. Варианты:

1. Другой администратор в разделе **«Аккаунты панели»**.
2. Скрипт: `python scripts/reset_admin_password.py --login admin --password 'NewPassword123'`.
3. Страница `/reset-password?token=…` — смена по одноразовому токену (таблица `password_reset_tokens`).

## 5. События и аудит

Подробно: [AUDIT_LOGGING.md](AUDIT_LOGGING.md).

**Кратко:**
- Файл событий (`ADMIN_EVENTS_LOG_PATH` / `data/bot.log`) — строки бота + `[ADMIN]` от панели (вход, выход, Docker-операции).
- Файл аудита (`ADMIN_AUDIT_LOG_PATH` / `data/admin_audit.log`) — отдельный журнал операций панели.
- Страница `/events` — таблица по хвосту файла (`ADMIN_EVENTS_LOG_SCAN_BYTES`, по умолчанию 8 МБ), фильтр по датам, CSV-экспорт.

## 6. Локальные команды разработчика

Когда Postgres доступен с хоста (Compose поднят, порт 5433):

```bash
export DATABASE_URL=postgresql://bot:<ПАРОЛЬ>@127.0.0.1:5433/via
export APP_MASTER_KEY=<32-символьный ключ>
pip install -r requirements.txt -r requirements-test.txt
python -m alembic upgrade head
python -m pytest tests/ -q --tb=short --ignore=tests/e2e
```

Если `DATABASE_URL` не задан, часть тестов будет пропущена — это нормально.

## 7. Типичные проблемы

### `password authentication failed for user "bot"`

Том `postgres_data` создан раньше с другим `POSTGRES_PASSWORD`. Варианты:

- Подставить в `DATABASE_URL` точно значение из текущего `.env`.
- Выровнять пароль: `docker compose exec postgres psql -U bot -d via -c "ALTER USER bot WITH PASSWORD 'новый_пароль';"`
- Для чистого dev: `docker compose down -v` (данные БД удалятся).

### `ERR_CONNECTION_REFUSED` на `127.0.0.1`

Админка слушает порт **8080** (`ADMIN_PORT` в `.env`). Полный URL: `http://127.0.0.1:8080/login`.

Если не работает:
```bash
docker compose ps
docker compose logs --tail=80 admin
```

### Бот спамит старыми уведомлениями после перезапуска

Очистите state:
```bash
docker compose exec postgres psql -U bot -d via -c "delete from bot_issue_state;"
docker compose restart bot
```

## 8. Остановка и бэкап

```bash
docker compose down
```

Данные БД в томе `postgres_data` при `down` **не удаляются**. Бэкапы — `pg_dump`.

Аварийный откат: [rollback-runbook.md](rollback-runbook.md).
