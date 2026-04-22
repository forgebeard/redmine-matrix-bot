# Миграция с NOTIFY_TEMPLATE_HTML_* / PLAIN_* на tpl v2

Раньше тексты для типов уведомлений из пути `processor` → `send_safe` можно было переопределять через ключи в `cycle_settings`:

- `NOTIFY_TEMPLATE_HTML_{TYPE}` (например `NOTIFY_TEMPLATE_HTML_NEW`)
- `NOTIFY_TEMPLATE_PLAIN_{TYPE}`

где `{TYPE}` — верхний регистр значения `notification_type` (`new`, `status_change`, …).

Переопределения делаются в админке в таблице `notification_templates` и файлах `templates/bot/tpl_*.html.j2`, с тем же приоритетом override в БД, что и для журнального движка.

## Что сделать оператору

1. Скопировать нужные фрагменты из старых полей onboarding (если они были заполнены) в соответствующий шаблон:
   - `new` / `reopened` → `tpl_new_issue`
   - `info`, `overdue`, `issue_updated`, `status_change` → `tpl_task_change`
   - `reminder` → `tpl_reminder`
2. Проверить предпросмотр в админке.
3. Ключи `NOTIFY_TEMPLATE_*` в `cycle_settings` удалены в ходе миграции на tpl-контур уведомлений; API **`/api/bot/content`** больше не отдаёт и не сохраняет этот JSON. В текущем дереве проекта ориентируйтесь на фактические ревизии в `alembic/versions/` (исторические номера `0021/0022` сохранены только как контекст).

## Legacy-код

Старый путь `notification.html` и чтение `NOTIFY_TEMPLATE_*` для Matrix удалены; остаётся только tpl v2.
