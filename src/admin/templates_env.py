"""Jinja2 / Starlette templates для админки."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TEMPLATES_DIR = str(REPO_ROOT / "templates" / "admin")


def admin_asset_version() -> str:
    """Query string для cache-bust ссылок на `/static/...` (см. `ADMIN_ASSET_VERSION`)."""
    v = (os.getenv("ADMIN_ASSET_VERSION") or "").strip()
    return v if v else "1"


# В некоторых наборах версий Jinja2/Starlette кэш шаблонов может приводить к TypeError
# (unhashable type: 'dict'). Отключаем кэш, чтобы /login работал стабильно.
_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=True,
    cache_size=0,
)
_jinja_env.globals["asset_version"] = admin_asset_version
_jinja_env.globals["bot_timezone"] = lambda: (os.getenv("BOT_TIMEZONE") or "Europe/Moscow")

templates = Jinja2Templates(env=_jinja_env)
