"""
Веб-админка: пользователи бота и маршруты Matrix (Postgres).

Запуск: uvicorn admin_main:app --host 0.0.0.0 --port 8080
Требуется DATABASE_URL (доступ к UI — через логин и пароль администратора).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from admin.csp import admin_csp_value as _admin_csp_value  # noqa: F401
from admin.csp import security_headers_middleware
from admin.lifespan import admin_lifespan as _admin_lifespan
from admin.middleware.auth import AuthMiddleware
from admin.routers.auth import router as auth_router
from admin.routers.dashboard import router as dashboard_router
from admin.routers.groups import router as groups_router
from admin.routers.health import router as health_router
from admin.routers.matrix_bind import router as matrix_bind_router
from admin.routers.me import router as me_router
from admin.routers.ops import router as ops_router
from admin.routers.redmine import router as redmine_router
from admin.routers.routes_cfg import router as routes_cfg_router
from admin.routers.secrets import router as secrets_router
from admin.routers.users import router as users_router
from admin.templates_env import admin_asset_version as _admin_asset_version  # noqa: F401

app = FastAPI(
    title="Matrix bot control panel",
    version="0.1.0",
    lifespan=_admin_lifespan,
)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(ops_router)
app.include_router(secrets_router)
app.include_router(groups_router)
app.include_router(users_router)
app.include_router(redmine_router)
app.include_router(routes_cfg_router)
app.include_router(matrix_bind_router)
app.include_router(me_router)
app.include_router(dashboard_router)
app.middleware("http")(security_headers_middleware)

_STATIC_ROOT = _ROOT / "static"
if _STATIC_ROOT.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_ROOT)), name="static")

app.add_middleware(AuthMiddleware)
