# Via v2.1 — журнальный движок (реализация)

## Включение

В таблице `cycle_settings` ключ **`JOURNAL_ENGINE_ENABLED`**: значения `1`, `true`, `on` включают журнальный цикл вместо legacy `check_user_issues` по пользователям.

Проверка флага выполняется в начале каждого тика [`check_all_users`](../src/bot/scheduler.py): при включённом флаге вызывается [`run_journal_tick`](../src/bot/journal_tick.py), цикл по пользователям не запускается.

## Порядок одного тика

1. **Drain** [`pending_digests`](../src/database/models.py) — [`drain_pending_digests`](../src/bot/digest_service.py), лимит **`DRAIN_MAX_USERS_PER_TICK`**.
2. **Фаза A** — глобальный запрос задач по `updated_on` и водяному знаку **`LAST_ISSUES_POLL_AT`** ([`phase_a_candidates`](../src/bot/journal_pipeline.py)).
3. **Фаза B** — догрузка задачи с `journals`/`watchers`, синхронизация [`bot_watcher_cache`](../src/database/models.py), новые журналы по [`bot_issue_journal_cursor`](../src/database/models.py).
4. **Обработчики** — для каждого нового журнала [`handle_journal_entry`](../src/bot/journal_handlers.py): маршрут [`get_matching_route`](../src/bot/routing.py), шаблоны Jinja2 (первый журнал задачи — `tpl_new_issue`, далее `tpl_task_change`), DND → строки digest; после цикла по журналам кандидата **один раз** [`update_reminder_timers`](../src/bot/reminder_service.py) по финальному статусу задачи (повторный `issue.get` в тике).
5. **Напоминания** — [`process_reminders`](../src/bot/reminder_service.py): строки `bot_issue_state` с прошедшим `*_reminder_due_at` и `reminder_count < MAX_REMINDERS`, шаблон `tpl_reminder`.
6. **DLQ** — [`retry_dlq_notifications`](../src/bot/scheduler.py) после п.5; размер пачки **`DLQ_BATCH_SIZE`**.

### Водяной знак vs курсор журнала

- **`LAST_ISSUES_POLL_AT`** — верхняя граница **глобального** поллинга Redmine в фазе A: какие задачи вообще попадают в выборку по `updated_on`. Продвигается в [`persist_watermark`](../src/bot/journal_pipeline.py) по максимальному `updated_on` среди полученных страниц (даже если часть задач вне scope исполнителя/наблюдателей).
- **`bot_issue_journal_cursor.last_journal_id`** — **по одной задаче**: до какого `journal_id` уже обработаны уведомления. Для каждого нового журнала после обработки вызывается [`advance_cursor_after_journal`](../src/bot/journal_pipeline.py) и `commit`. Ошибка рендера/отправки не откатывает курсор назад: запись уходит в DLQ, курсор двигается вперёд (at-most-once с подстраховкой оператора через DLQ).

### Зафиксированные политики (продукт)

1. **Курсор при ошибке доставки/рендера:** DLQ + курсор вперёд; исключение наружу из `journal_tick` не поднимается из цепочки render+send в [`journal_render_send_or_dlq`](../src/bot/journal_handlers.py).
2. **DLQ при ошибке рендера (A1):** в `pending_notifications.payload` — только JSON-сериализуемые поля, флаг `needs_rerender: true`, контекст для Jinja (`template_name`, `jinja_context`, `plain_body`, …). При retry сначала повторный рендер, затем отправка готового HTML; сырой текст `[render error]` в комнату не является основным сценарием.
3. **Таймеры напоминаний:** ориентир `issue.status.is_closed` из Redmine; при закрытии — `*_reminder_due_at = NULL`, `reminder_count = 0`; при открытом и при **любой** смене статуса между незакрытыми — оба `*_reminder_due_at = now + DEFAULT_REMINDER_INTERVAL`, `reminder_count = 0`. Вызов **`update_reminder_timers` ровно один раз на кандидата** после цикла `for journal in new_journals`, на объекте задачи с актуальным статусом после обработки журналов.
4. **Self-action и наблюдатели:** автор журнала не получает **персональные** уведомления (в т.ч. если он watcher); **групповой** канал не подавляется. Дедуп личных получателей по `bot_users.id` (пересечение исполнитель ∩ наблюдатель и т.п.).

### Формат DLQ

- **Ошибка после успешного рендера** (сеть/Matrix): `payload` = готовое тело `m.room.message` (`msgtype`, `body`, `format`, `formatted_body`).
- **Ошибка до готового тела** (шаблон/рендер): см. п.2 выше (`needs_rerender`), в `payload` — JSON-safe поля для повторного рендера (`template_name`, `jinja_context`, `plain_body`, …).
  - **Контекст — снимок на момент события**, не актуальное состояние задачи в Redmine (live-state). При retry задача не перечитывается для обновления контекста; это уведомление о прошедшем событии журнала (согласовано с планом A1).

## Чеклист staging

1. Включить `JOURNAL_ENGINE_ENABLED`, убедиться что legacy-цикл по пользователям не выполняется.
2. Сломать шаблон в БД → в `pending_notifications` строка с `needs_rerender`, JSON-валидный `payload`; после исправления шаблона — успешный `retry_dlq_notifications`.
3. Проверить продвижение `LAST_ISSUES_POLL_AT` и `bot_issue_journal_cursor` на тестовой задаче.
4. Reassign + watcher: персональные сообщения нужным комнатам, без дубликатов; автор журнала не получает личное при self.
5. DND: события уходят в `pending_digests`, drain отдаёт сгруппированный по `issue_id` дайджест.
6. Напоминания: после простоя — `tpl_reminder`, счётчики и `*_reminder_due_at` обновляются; закрытие задачи обнуляет таймеры.

## Ключи cycle_settings

| Ключ | Назначение |
|------|----------------|
| `LAST_ISSUES_POLL_AT` | Водяной знак фазы A (ISO-8601 UTC) |
| `MAX_ISSUES_PER_TICK` | Лимит задач на страницу Redmine |
| `MAX_PAGES_PER_TICK` | Макс. страниц за тик |
| `WATCHER_CACHE_REFRESH_EVERY_N_TICKS` | Полный refresh кэша наблюдателей каждые N тиков |
| `CHECK_INTERVAL` | Интервал тика планировщика (сек), участвует в пороге очистки устаревших строк watcher cache: `max(24ч, 2 * N * CHECK_INTERVAL)` |
| `DEFAULT_REMINDER_INTERVAL` | Интервал напоминаний по застою (сек) |
| `MAX_REMINDERS` | Максимум отправок напоминаний по одной паре user/issue |
| `DLQ_BATCH_SIZE` | Макс. строк DLQ за один `retry_dlq_notifications` |
| `MAX_DLQ_RETRIES` | Лимит повторов по одной записи DLQ |
| `DRAIN_MAX_USERS_PER_TICK` | Лимит пользователей на drain digest |
| `JOURNAL_ENGINE_ENABLED` | Вкл/выкл журнального движка |

Сиды добавлены миграцией [`0021_journal_engine_v2`](../alembic/versions/20260418_0021_journal_engine_v2.py).

## Шаблоны

- Файлы по умолчанию: `templates/bot/tpl_*.html.j2`.
- Override в БД: таблица `notification_templates`; `NULL` в `body_html` — брать файл.
- Админка: вкладка «Уведомления» → блок **шаблоны журнального движка (v2)**; API `/api/bot/notification-templates`.

Миграция переносит непустые `NOTIFY_TEMPLATE_HTML_*` из `cycle_settings` в `notification_templates` (имена `tpl_new_issue`, `tpl_task_change`, `tpl_reminder`), без перезаписи уже существующих строк.

## Маршрутизация

Конфиг расширенного роутинга попадает в [`bot.config_state.ROUTING`](../src/bot/config_state.py) из [`fetch_runtime_config`](../src/database/load_config.py) (пятое значение кортежа).

## Миграции

Ревизия **0021_journal_engine_v2**: курсор журнала, digest, watcher cache, шаблоны, колонки маршрутов, поля `bot_issue_state`, `support_groups.notify_on_assignment`.
