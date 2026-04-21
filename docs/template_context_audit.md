# Аудит контекста Jinja2-шаблонов уведомлений (v2)

Дата: после рефакторинга контекста. Цель — зафиксировать контракты между кодом, шаблонами в `templates/bot/*.html.j2` и предпросмотром в админке.

## Принятые продуктовые решения

1. **`tpl_digest`** — отдельная модель контекста: только `{"items": [...]}`. Не прогоняется через `build_issue_context` (нет N запросов к Redmine, не раздуваем digest-очередь). Поля элементов согласованы с [`tpl_digest.html.j2`](../templates/bot/tpl_digest.html.j2) (`issue_id`, `subject`, `events` или `event_type`).
2. **`tpl_test_message`** — отдельный шаблон для тестовой отправки из админки (`/users/test-message`, `/groups/test-message`), не смешивается с рабочими event-шаблонами.
3. **`tpl_dry_run`** — выведен из runtime-контракта (удалён из реестра, API и шаблонов); любые старые override-строки считаются orphan и очищаются миграцией.
4. **`sandbox_accepts_context`** в [`template_loader.py`](../src/bot/template_loader.py) — вынесено в бэклог: сейчас парсит только дефолтный файл с диска, не override из БД.

## Вызовы `render_named_template`

| Место | Шаблон | Контекст |
|-------|--------|----------|
| [`journal_handlers.journal_render_send_or_dlq`](../src/bot/journal_handlers.py) | `tpl_new_issue` / `tpl_task_change` / `tpl_reminder` (через вызывающих) | `build_issue_context` + `**extra` |
| [`digest_service.drain_pending_digests`](../src/bot/digest_service.py) | `tpl_digest` | `{"items": [...]}` только |
| [`scheduler.retry_dlq_notifications`](../src/bot/scheduler.py) | из `payload.template_name` | `payload.jinja_context` (снимок из DLQ) |

Предпросмотр в админке: [`notification_templates.py`](../src/admin/routes/notification_templates.py) — `SandboxedEnvironment.from_string(...).render(**ctx)`, не `render_named_template`. Контекст для issue-шаблонов синхронизирован с ключами `build_issue_context` / `preview_issue_context_demo`.

## Таблица: шаблон — прод — ожидание `.j2` — разрыв (до рефакторинга)

| Шаблон | Передавалось в прод | Ожидает `.j2` | Разрыв |
|--------|---------------------|---------------|--------|
| `tpl_task_change` | `issue_id`, `issue_url`, `subject`, `event_type`, `extra_text`, `title`, `emoji` | те же + дефолты в шаблоне | Нет |
| `tpl_new_issue` | **тот же dict**, что и для `tpl_task_change` | + `status`, `priority`, `version` | **Не хватало** `status`, `priority`, `version` — исправлено через `build_issue_context` |
| `tpl_reminder` | частичный набор + `reminder_text` | `issue_url`, `issue_id`, `subject`, `reminder_text` | Лишние ключи не мешают; полный контекст добавлен для согласованности |
| `tpl_digest` | `items[]` с `issue_id`, `subject`, `events` | цикл по `items` | Отдельный контракт; без `build_issue_context` на корне |
| `tpl_test_message` | админский test-message роут | `title`, `message`, `sent_at`, `timezone`, `scope` | Не в event-пайплайне бота |

## Контракт `render_named_template` (после рефакторинга)

Возвращает `tuple[str, str | None]`: HTML и plain из Jinja по `body_plain` в БД; `None` — использовать fallback вызывающего (`plain_body` и т.д.).

## Контракт DLQ `needs_rerender`

`jinja_context` — JSON-serиализуемый снимок на момент события; см. [`TZ_BOT_V2_IMPLEMENTATION.md`](TZ_BOT_V2_IMPLEMENTATION.md). После сериализации — проверка `json.dumps` + при сбое shallow-sanitize в [`journal_handlers.jinja_context_json_safe`](../src/bot/journal_handlers.py).
