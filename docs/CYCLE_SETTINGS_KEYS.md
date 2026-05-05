# Cycle Settings Keys

Сводка ключей `cycle_settings`, которые реально читаются runtime-кодом.

## Ключи, используемые в коде

| Ключ | Где читается | Назначение |
|------|--------------|------------|
| `CHECK_INTERVAL` | `bot/main.py`, `bot/config_hot_reload.py`, `bot/journal_tick.py` | Интервал основного цикла |
| `REMINDER_AFTER` | `bot/main.py`, `bot/config_hot_reload.py` | Legacy fallback для reminder |
| `GROUP_REPEAT_SECONDS` | `bot/main.py`, `bot/config_hot_reload.py` | Интервал повтора group-notify |
| `BOT_LEASE_TTL_SECONDS` | `bot/main.py`, `bot/config_hot_reload.py` | TTL lease координации |
| `BOT_TIMEZONE` | `bot/main.py`, `bot/config_hot_reload.py`, `admin/routes/settings.py` | Таймзона бота |
| `MATRIX_DEVICE_ID` | `bot/main.py`, `bot/config_hot_reload.py` | Device ID Matrix-клиента |
| `DAILY_REPORT_ENABLED` | `bot/main.py`, `bot/config_hot_reload.py` | Вкл/выкл daily-report job |
| `DAILY_REPORT_HOUR` | `bot/main.py`, `bot/config_hot_reload.py` | Час daily-report |
| `DAILY_REPORT_MINUTE` | `bot/main.py`, `bot/config_hot_reload.py` | Минута daily-report |
| `MAX_ISSUES_PER_TICK` | `bot/journal_tick.py` | Ограничение фазы A |
| `MAX_PAGES_PER_TICK` | `bot/journal_tick.py` | Ограничение страниц фазы A |
| `DRAIN_MAX_USERS_PER_TICK` | `bot/journal_tick.py` | Лимит drain digest |
| `WATCHER_CACHE_REFRESH_EVERY_N_TICKS` | `bot/journal_tick.py` | Частота refresh watcher cache |
| `DLQ_BATCH_SIZE` | `bot/journal_tick.py` | Batch size DLQ retry |
| `MAX_REMINDERS` | `bot/reminder_service.py`, `bot/sender.py` | Лимит напоминаний |
| `DEFAULT_REMINDER_INTERVAL` | `bot/reminder_service.py` | Интервал ремайндеров |

## Deprecated / неиспользуемые ключи

| Ключ | Статус |
|------|--------|
| `JOURNAL_ENGINE_ENABLED` | В актуальном `src/` не читается. Исторический маркер из legacy-доков; не переключает кодовые ветки. |

## Примечание

Этот документ фиксирует фактические чтения из кода. Изменения ключей делать только после синхронизации с [docs/TZ_BOT_V2_IMPLEMENTATION.md](TZ_BOT_V2_IMPLEMENTATION.md) и [docs/JOURNAL_ENGINE_AND_SENDER.md](JOURNAL_ENGINE_AND_SENDER.md).
