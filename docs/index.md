# Документация Via

## Current

| Документ | Описание |
|----------|----------|
| [README.md](../README.md) | Обзор проекта, быстрый старт, структура |
| [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) | Развёртывание на сервере (RHEL/AlmaLinux/Rocky) |
| [ADMINISTRATOR_GUIDE.md](ADMINISTRATOR_GUIDE.md) | Панель администратора, первый вход, troubleshooting |
| [notification_template_variables.md](notification_template_variables.md) | Поля Jinja по шаблонам уведомлений Matrix |
| [MATRIX_NOTIFICATION_V5.md](MATRIX_NOTIFICATION_V5.md) | Актуальный контракт Matrix-уведомлений v5 |
| [AUDIT_LOGGING.md](AUDIT_LOGGING.md) | Логирование и аудит действий в панели |
| [secrets-storage.md](secrets-storage.md) | Хранение секретов и шифрование |
| [rollback-runbook.md](rollback-runbook.md) | Аварийный откат |
| [ui-smoke-checklist.md](ui-smoke-checklist.md) | Smoke-чеклист UI перед merge |

## Design / Audit

| Документ | Описание |
|----------|----------|
| [ARCHITECTURE_ADMIN_DB_BOT.md](ARCHITECTURE_ADMIN_DB_BOT.md) | Архитектурный обзор admin/bot/db |
| [JOURNAL_ENGINE_AND_SENDER.md](JOURNAL_ENGINE_AND_SENDER.md) | Технические детали журналов и sender |
| [RUNTIME_ROUTING_CONFIG.md](RUNTIME_ROUTING_CONFIG.md) | Источники правды: маршруты комнат в runtime (БД → maps vs routes_config) |
| [CYCLE_SETTINGS_KEYS.md](CYCLE_SETTINGS_KEYS.md) | Поддерживаемые и deprecated ключи `cycle_settings` |
| [TZ_BOT_V2_IMPLEMENTATION.md](TZ_BOT_V2_IMPLEMENTATION.md) | Реализационные заметки по журналному движку (исторический контекст и текущие ключи) |
| [NOTIFY_TEMPLATE_MIGRATION.md](NOTIFY_TEMPLATE_MIGRATION.md) | История миграции старых NOTIFY_TEMPLATE ключей на tpl-контур |
| [template_context_audit.md](template_context_audit.md) | Контракт контекста Jinja-шаблонов |
| [ADR_unified_notification_templates.md](ADR_unified_notification_templates.md) | ADR по унификации шаблонов уведомлений |
| [AUDIT_notification_links_2026-04-21.md](AUDIT_notification_links_2026-04-21.md) | Аудит маршрутизации/ссылок (снимок) |
| [LOGGING_DUPLICATION_DIAGNOSIS_2026-04-21.md](LOGGING_DUPLICATION_DIAGNOSIS_2026-04-21.md) | Диагностика и устранение дублирования логов |
| [RUFF_BACKLOG.md](RUFF_BACKLOG.md) | Backlog исторических ruff-замечаний и порядок зачистки |

## Historical / Obsolete

| Документ | Статус |
|----------|--------|
| [TZ_notifications_admin_ui_matrix_preview.md](TZ_notifications_admin_ui_matrix_preview.md) | Obsolete: переходный период code/block editor |
| [TZ_notifications_ui_round2.md](TZ_notifications_ui_round2.md) | Obsolete: исторический раунд block-editor UX |
| [diagnostics/preview_loading_bug.md](diagnostics/preview_loading_bug.md) | Obsolete: инцидент миграции preview |

Конфигурация: [.env.example](../.env.example).
