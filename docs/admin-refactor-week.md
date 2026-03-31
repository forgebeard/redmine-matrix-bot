# План выноса админки из `admin_main.py` (неделя, внутренняя чистота)

## Сделано

- День 1: пакет `src/admin/` (`constants`, `templates_env`, `csrf`, `csp`, `lifespan`), `routers/health.py`.
- День 2–3: `runtime.py`, `session_logic.py`, `timeutil.py`, `middleware/auth.py` (сессии + CSRF cookie).
- День 4: `auth_helpers.py`, `routers/auth.py` (login, setup, onboarding, forgot/reset, logout).

## Дальше по дням

| День | Задача |
|------|--------|
| 5 | Роутеры `ops`, `users`, `routes`, `matrix_bind`, `me` — порциями |

## Инварианты

- Два процесса (bot + admin), URL и поведение без регрессий.
- Rate limit — in-memory, одна реплика админки.
- Миграции только Alembic вперёд.
