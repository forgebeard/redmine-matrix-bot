# Аудит: админка → БД → бот (этап 0 плана)

Дата: 2026-04-18. Цель: зафиксировать матрицу источников данных, разрывы с `.env` и сценарии удаления.

## 1. Матрица: экран / API админки → таблицы Postgres → что читает бот

| Область | Где в админке | Таблицы / хранилище | Как бот использует |
|--------|----------------|----------------------|---------------------|
| Интеграция Matrix + Redmine (URL, ключ, токены) | `/onboarding` ([`settings.onboarding_save`](../src/admin/routes/settings.py)), `/secrets` ([`secrets_save`](../src/admin/routes/secrets.py)) | `app_secrets` (`AppSecret`) | [`main.py`](../src/bot/main.py): ожидание всех имён `REDMINE_URL`, `REDMINE_API_KEY`, `MATRIX_HOMESERVER`, `MATRIX_ACCESS_TOKEN`, `MATRIX_USER_ID` |
| Сервисная таймзона (для админки при старте) | onboarding, секрет с именем `__service_timezone` (см. [`main.py`](../src/admin/main.py) `SERVICE_TIMEZONE_SECRET`) | `app_secrets` | Бот основную таймзону берёт из `cycle_settings` / каталогов после старта, не из этого ключа напрямую |
| Интервалы, таймзона бота; расписание утреннего отчёта; **Matrix device ID** — env / `cycle_settings` | [`/onboarding`](../src/admin/routes/settings.py), API [`/api/bot/content`](../src/admin/routes/bot_content.py) (только `DAILY_REPORT_*` расписание) | `cycle_settings` (`CycleSettings`); ключи в т.ч. `BOT_TIMEZONE`, `MATRIX_DEVICE_ID`, `DAILY_REPORT_ENABLED` / `HOUR` / `MINUTE` | [`load_catalogs`](../src/bot/catalogs.py) + [`fetch_cycle_settings`](../src/database/load_config.py) в [`main.py`](../src/bot/main.py) |
| Тексты Matrix и утреннего отчёта (tpl v2) | вкладка «Уведомления» onboarding, API [`/api/bot/notification-templates`](../src/admin/routes/notification_templates.py) | `notification_templates` + файлы `templates/bot/tpl_*.html.j2` | [`render_named_template`](../src/bot/template_loader.py), [`scheduler.daily_report`](../src/bot/scheduler.py) для `tpl_daily_report` |
| Пользователи бота | `/users` | `bot_users` (`BotUser`), опционально ключ в колонках ciphertext | [`fetch_runtime_config`](../src/database/load_config.py) |
| Группы поддержки | `/groups` | `support_groups`, `group_version_routes` | то же |
| Маршруты: статус→комната | `/groups` (формы `/groups/{id}/status-routes/*` в [`groups.py`](../src/admin/routes/groups.py)) | `status_room_routes` | `fetch_runtime_config` |
| Маршруты: версия→комната (глобально) | `/settings/routes/version` ([`routes_mgmt`](../src/admin/routes/routes_mgmt.py)) | `version_room_routes` | `fetch_runtime_config` |
| Доп. маршруты версий | формы пользователя/группы | `user_version_routes`, `group_version_routes` | то же |
| Справочники Redmine | каталог в админке [`catalog`](../src/admin/routes/catalog.py) | `redmine_statuses`, `redmine_versions`, `redmine_priorities`, `notification_types` | [`load_catalogs`](../src/bot/catalogs.py) |
| Аккаунты панели (логин) | `/app-users` и др. | `bot_app_users`, `bot_sessions`, … | Не используются ботом для рассылки |
| Heartbeat | бот POST [`/api/bot/heartbeat`](../src/admin/routes/users.py) | `bot_heartbeat` | Только мониторинг |
| Очередь доставки Matrix (thin worker) | GET [`/api/bot/commands`](../src/admin/routes/bot_runtime.py), POST ack/error | `pending_notifications` (отдельной таблицы «команд» нет) | [`command_worker`](../src/bot/command_worker.py): pull из API; та же DLQ, что и retry в монолитном боте |

## 2. Что бот всё ещё берёт из окружения / [`config.py`](../src/config.py) (не из «мозга» БД)

### 2.1. Непосредственно в процессе бота (`src/bot/`)

| Переменная / источник | Назначение | Заметка |
|----------------------|------------|---------|
| `BOT_INSTANCE_ID` | UUID инстанса | Инфраструктура |
| `BOT_RUNTIME_STATUS_FILE` | путь к `runtime_status.json` | Инфраструктура |
| `ADMIN_URL` | HTTP к админке: команды + heartbeat | Инфраструктура; должен указывать на тот же «мозг» |

### 2.2. Через импорт [`config.py`](../src/config.py) (загрузка при старте модуля)

Пути логов (`LOG_*`), `MATRIX_DEVICE_ID`, retry Matrix (`MATRIX_RETRY_*`), `CHECK_INTERVAL` / `REMINDER_AFTER` / … как **дефолты до** перезаписи из БД в `main()`; `CONFIG_POLL_INTERVAL_SEC`, `COMMAND_POLL_INTERVAL_SEC`, `HEARTBEAT_INTERVAL_SEC`, `BOT_LEASE_TTL_SECONDS` — пока без UI в `cycle_settings` для части из них.

В [`config.py`](../src/config.py) имена `USERS` / `STATUS_ROOM_MAP` / `VERSION_ROOM_MAP` оставлены пустыми (не читаются из `.env`); источник правды — Postgres и `bot.main`. Периодическая подгрузка без рестарта: [`config_hot_reload.py`](../src/bot/config_hot_reload.py), env `BOT_HOT_RELOAD` / `BOT_HOT_RELOAD_INTERVAL_SEC`.

### 2.3. Риск рассинхрона

- **Смягчено:** единая функция `effective_bot_timezone_for_admin` ([`helpers_ext.py`](../src/admin/helpers_ext.py)) выставляет `BOT_TIMEZONE` при старте админки и после сохранения onboarding: приоритет `cycle_settings.BOT_TIMEZONE` → секрет `__service_timezone` → env. Сохранение формы onboarding дополнительно пишет `BOT_TIMEZONE` в `cycle_settings` и дублирует в `__service_timezone`.

## 3. Удаление в админке и целостность данных

### 3.1. Реализованные удаления (строки реально уходят из БД)

- **Пользователь бота** [`users_delete`](../src/admin/routes/users.py) / bulk-delete: `DELETE` из `bot_users`. Дочерние `user_version_routes` — **CASCADE** по FK ([`models.py`](../src/database/models.py)).
- **Группа** [`groups_delete`](../src/admin/routes/groups.py): удаление `support_groups`; у пользователей `group_id` → **SET NULL**; `group_version_routes` — **CASCADE**.
- **Глобальные маршруты** [`routes_mgmt`](../src/admin/routes/routes_mgmt.py): явный `DELETE` по id строки.
- **Маршруты версий** у пользователя/группы — отдельные POST `.../delete`.
- **Каталог** Redmine: [`catalog_*_delete`](../src/admin/routes/catalog.py) — `DELETE` строки справочника.

### 3.2. Пробелы и закрытые моменты

| Тема | Статус |
|------|--------|
| **Секреты** | Удаление строки `app_secrets` — POST [`/secrets/delete`](../src/admin/routes/secrets.py), кнопка в [`secrets.html`](../templates/admin/panel/secrets.html); аудит CRUD при включённом флаге. |
| **State / lease / DLQ при удалении пользователя** | При удалении [`bot_users`](../src/admin/routes/users.py) вызывается [`delete_runtime_data_for_redmine_user`](../src/database/user_runtime_cleanup.py): очистка `bot_issue_state`, `pending_notifications`, `bot_user_leases` по `user_redmine_id`. |
| **Не-SQL** | Журнал `/events` (файл), статус бота (Docker + при необходимости `runtime_status.json`) — по-прежнему вне таблиц конфигурации. |

### 3.3. Вывод

Для маршрутизации, каталога, секретов и удаления пользователя бота цепочка «UI → БД → согласованные данные» **приведена к ожидаемому виду** для перечисленного выше. Исключения — осознанные (файловый журнал, инфраструктурный статус).

Сводная статья для людей: [ARCHITECTURE_ADMIN_DB_BOT.md](ARCHITECTURE_ADMIN_DB_BOT.md).

## 4. История заметок (аудит)

Ранее здесь был список «следующих задач»; часть пунктов выполнена (секреты, очистка при удалении пользователя, см. §3.2). Актуальные операционные шаги: [`ADMINISTRATOR_GUIDE.md`](ADMINISTRATOR_GUIDE.md).

---

*Документ можно обновлять по мере рефакторинга; ссылка на него в roadmap плана «admin = brain, bot = hands».*
