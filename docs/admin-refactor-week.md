# План выноса админки из `admin_main.py` (неделя, внутренняя чистота)

## Сделано

- День 1: пакет `src/admin/` (`constants`, `templates_env`, `csrf`, `csp`, `lifespan`), `routers/health.py`.
- День 2–3: `runtime.py`, `session_logic.py`, `timeutil.py`, `middleware/auth.py` (сессии + CSRF cookie).

## Дальше по дням

| День | Задача |
|------|--------|
| 4 | Роутер `routers/auth.py` (login, setup, forgot/reset, logout, onboarding) |
| 5 | Роутер `users` / `ops` / остальное порциями; тонкий `admin_main` |

## Инварианты

- Два процесса (bot + admin), URL и поведение без регрессий.
- Rate limit — in-memory, одна реплика админки.
- Миграции только Alembic вперёд.
