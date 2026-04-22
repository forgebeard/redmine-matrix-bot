# ADR: единый источник текстов Matrix-уведомлений (`tpl_*`)

## Контекст

Бот собирал тело сообщения несколькими путями (старый `notification.html`, `NOTIFICATION_TYPES`, `cycle_settings.NOTIFY_TEMPLATE_*`, отдельно `tpl_*` в журнальном движке). Цель — для пути `send_safe` / `build_matrix_message_content` использовать те же именованные шаблоны, что и админка (`notification_templates` + файлы `templates/bot/tpl_*.html.j2`). Legacy-ветка и переменная отката удалены.

## Решение по DLQ (фаза 0)

**Выбран минимальный вариант:** в DLQ по-прежнему сохраняется готовый `payload` (`m.room.message`), но он собирается тем же кодом, что и успешная отправка. После перехода на `render_named_template` payload отражает актуальный HTML/plain из `tpl_*` (с учётом override в БД).

Расширенный вариант (хранить только `template_name` + `jinja_context` в DLQ) **не** принимается в текущей итерации — меньше изменений в `dlq_repo` и retry-логике.

## Утренний отчёт (`daily_report`)

Текст сообщения рендерится через **`tpl_daily_report`** (`render_named_template` + таблица `notification_templates` + файл `templates/bot/tpl_daily_report.html.j2`), как и остальные tpl. Расписание (`DAILY_REPORT_ENABLED`, `DAILY_REPORT_HOUR`, `DAILY_REPORT_MINUTE`) остаётся в **`cycle_settings`**; правка выполняется из onboarding. Шаблоны HTML/plain отчёта в `cycle_settings` (`DAILY_REPORT_HTML_TEMPLATE` / `PLAIN`) удалены при переходе на tpl-контур (историческая миграция; в текущем дереве ориентироваться на фактические ревизии в `alembic/versions/`). Тип `daily_report` по-прежнему **не** входит в `EVENT_TO_TEMPLATE` (отдельный путь `scheduler.daily_report`).

## Ручной чеклист перед релизом (Matrix + админка)

Автотесты покрывают маппинг `EVENT_TO_TEMPLATE` и DLQ; ниже — что проверить вручную на стенде.

1. **Matrix по типам** — для каждого ключа из `EVENT_TO_TEMPLATE` (`new`, `reopened`, `info`, `reminder`, `overdue`, `issue_updated`, `status_change`) вызвать сценарий, который даёт одно реальное сообщение в комнату, и убедиться: верстка осмысленна, ссылка на задачу открывается, для `reminder`/`overdue` текст соответствует ожиданиям.
2. **Override без деплоя** — в админке изменить HTML/plain для одного `tpl_*`, сохранить, повторить отправку того же типа: в Matrix должно прийти с новым текстом (чтение из `notification_templates`).
3. **Утренний отчёт** — проверить срабатывание по расписанию и вид сообщения в Matrix; при необходимости править `tpl_daily_report` в конструкторе на вкладке «Уведомления».

## Дата

Введено в рамках плана унификации шаблонов (см. `.cursor/plans/unify_notifications_tpl_*.plan.md`). Режим `USE_LEGACY_TEMPLATES` удалён после стабилизации tpl-only пути.

## Update (v5 cutover)

- Для `issue_updated` и `status_change` используется каноничный v5-формат карточки в `tpl_task_change`.
- Legacy путь обновлений в `processor` исключён из боевого контура; рабочий контур — журналный движок.
- Plain fallback для v5-карточки формируется кодом (`| ` префикс по строкам), не отдельным `.plain.j2`.

## Update (admin editor: code-only)

- В админке удалён block-editor runtime-path: вкладка `Уведомления` использует единый редактор кода с live-preview.
- Для preview сохранён единый endpoint `POST /api/bot/notification-templates/preview` (debounce + защита от гонок в UI).
- Удалённые block-endpoints (`/compile-blocks`, `/{name}/decompose`, `/{name}/decompose-body`, `/block-registry`) возвращают `404`.
- Миграция БД не требуется: используется прежняя модель `notification_templates.body_html` (default из файлов + custom override в БД).
