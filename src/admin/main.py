"""
Веб-админка: FastAPI приложение.

Все helpers доступны через _admin() = import admin.main.
Имена экспортируются явно через admin._exports.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from fastapi import FastAPI  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

from admin.helpers import _jinja_env  # noqa: E402
from database.session import get_session_factory  # noqa: E402
from logging_config import setup_json_logging  # noqa: E402
from security import SecurityError, load_master_key  # noqa: E402

SERVICE_TIMEZONE_SECRET = "__service_timezone"
SERVICE_TIMEZONE_FALLBACK = "Europe/Moscow"

logger = logging.getLogger("admin")


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    setup_json_logging("admin")
    logger.info("🚀 Admin panel starting up...")
    try:
        load_master_key()
    except SecurityError as e:
        raise RuntimeError(f"startup failed: {e}") from e
    try:
        from admin._exports import _load_secret_plain, _normalize_service_timezone_name

        factory = get_session_factory()
        async with factory() as session:
            tz_saved = await _load_secret_plain(session, SERVICE_TIMEZONE_SECRET)
        os.environ["BOT_TIMEZONE"] = _normalize_service_timezone_name(tz_saved)
    except Exception:
        logger.warning("service_timezone_load_failed", exc_info=True)
    logger.info("✅ Admin panel ready")
    yield
    logger.info("👋 Admin panel shutting down")


app = FastAPI(title="Matrix bot control panel", version="0.1.0", lifespan=_app_lifespan)

_STATIC_ROOT = _ROOT / "static"
if _STATIC_ROOT.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_ROOT)), name="static")

# ── Re-export helpers (для _admin() из route файлов) ─────────────────────────

from admin._exports import *  # noqa: E402, F401, F403

# Явные импорты для использования в main.py (ruff F405)
from admin._exports import (  # noqa: E402
    GROUP_UNASSIGNED_DISPLAY,
    GROUP_UNASSIGNED_NAME,
    GROUP_USERS_FILTER_ALL_LABEL,
    AuthMiddleware,
    CspSecurityMiddleware,
)

# ── Middleware ──────────────────────────────────────────────────────────────

app.add_middleware(CspSecurityMiddleware)
app.add_middleware(AuthMiddleware)

# ── NOTIFY_TYPE_KEYS ─────────────────────────────────────────────────────────

NOTIFY_TYPE_KEYS: list[str] = []

# ── Routers ──────────────────────────────────────────────────────────────────

from admin.db_config import router as db_config_router  # noqa: E402
from admin.routes.app_users import router as app_users_router  # noqa: E402
from admin.routes.auth import router as auth_router  # noqa: E402
from admin.routes.dashboard import router as dashboard_router  # noqa: E402
from admin.routes.events import router as events_router  # noqa: E402
from admin.routes.groups import router as groups_router  # noqa: E402
from admin.routes.health import router as health_router  # noqa: E402
from admin.routes.me import router as me_router  # noqa: E402
from admin.routes.ops import router as ops_router  # noqa: E402
from admin.routes.redmine import router as redmine_router  # noqa: E402
from admin.routes.routes_mgmt import router as routes_mgmt_router  # noqa: E402
from admin.routes.secrets import router as secrets_router  # noqa: E402
from admin.routes.settings import router as settings_router  # noqa: E402
from admin.routes.user_import import router as user_import_router  # noqa: E402
from admin.routes.users import router as users_router  # noqa: E402

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(ops_router)
app.include_router(dashboard_router)
app.include_router(events_router)
app.include_router(settings_router)
app.include_router(me_router)
app.include_router(redmine_router)
app.include_router(secrets_router)
app.include_router(app_users_router)
app.include_router(routes_mgmt_router)
app.include_router(groups_router)
app.include_router(users_router)
app.include_router(user_import_router)
app.include_router(db_config_router)

# ── Jinja2 globals ───────────────────────────────────────────────────────────

_jinja_env.globals["GROUP_UNASSIGNED_NAME"] = GROUP_UNASSIGNED_NAME
_jinja_env.globals["GROUP_UNASSIGNED_DISPLAY"] = GROUP_UNASSIGNED_DISPLAY
_jinja_env.globals["GROUP_USERS_FILTER_ALL_LABEL"] = GROUP_USERS_FILTER_ALL_LABEL
_jinja_env.globals["dashboard_path"] = "/dashboard"
