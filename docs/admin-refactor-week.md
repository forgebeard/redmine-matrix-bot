# План выноса админки из `admin_main.py` (неделя, внутренняя чистота)

## Сделано

- День 1: пакет `src/admin/` (`constants`, `templates_env`, `csrf`, `csp`, `lifespan`), `routers/health.py`.
- День 2–3: `runtime.py`, `session_logic.py`, `timeutil.py`, `middleware/auth.py` (сессии + CSRF cookie).
- День 4: `auth_helpers.py`, `routers/auth.py` (login, setup, onboarding, logout).
- День 5: `audit.py`, `routers/ops.py`, `routers/secrets.py`.
- День 6: `routers/app_users.py` (позже удалён: один админ, смена учётки через CLI).
- День 7: `notify_prefs.py`, `matrix_tokens.py`, `MATRIX_CODE_TTL_SECONDS` в `constants`; роутеры `groups`, `users`, `redmine`, `routes_cfg` (статус/версия), `matrix_bind`, `me`; в `admin_main.py` — только сборка `app` и подключение роутеров.
- День 8: `authz.require_admin`, `routers/dashboard.py` для `/`; проверки в `secrets`, `ops`.
- Логин вместо email: миграция `0008_login_auth`, `admin/cli_admin_credentials.py`, без веб-сброса пароля.

## Дальше по дням

| День | Задача |
|------|--------|
| 9+ | По необходимости: FastAPI `Depends(require_admin_dep)` вместо вызова в теле хендлера; мелкая зачистка |

## Инварианты

- Два процесса (bot + admin), URL и поведение без регрессий.
- Rate limit — in-memory, одна реплика админки.
- Миграции только Alembic вперёд.
