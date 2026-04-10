"""Shared helpers for admin routes."""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from cachetools import TTLCache
from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from database.session import get_session_factory
from mail import mask_identifier

logger = logging.getLogger("redmine_admin")

# ── Templates ────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parents[2]
_templates_dir = str(_ROOT / "templates" / "admin")
_jinja_env = Environment(loader=FileSystemLoader(_templates_dir), autoescape=True, cache_size=0)
templates = Jinja2Templates(env=_jinja_env)


def _admin_asset_version() -> str:
    v = (os.getenv("ADMIN_ASSET_VERSION") or "").strip()
    return v if v else "6"


_jinja_env.globals["asset_version"] = _admin_asset_version
_jinja_env.globals["bot_timezone"] = lambda: (os.getenv("BOT_TIMEZONE") or "Europe/Moscow")

# ── Config constants ────────────────────────────────────────────────────────

SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE", "admin_session")
CSRF_COOKIE_NAME = os.getenv("ADMIN_CSRF_COOKIE", "admin_csrf")
ADMIN_PORT = int(os.getenv("ADMIN_PORT", "8080"))
COOKIE_SECURE = bool(int(os.getenv("COOKIE_SECURE", "0")))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
RESET_TOKEN_TTL_SECONDS = int(os.getenv("RESET_TOKEN_TTL_SECONDS", "1800"))
RESET_COOLDOWN_SECONDS = int(os.getenv("RESET_COOLDOWN_SECONDS", "90"))
AUTH_TOKEN_SALT = os.getenv("AUTH_TOKEN_SALT", "dev-change-me-in-prod")
SHOW_DEV_TOKENS = bool(int(os.getenv("SHOW_DEV_TOKENS", "0")))

SERVICE_TIMEZONE_FALLBACK = "Europe/Moscow"
SERVICE_TIMEZONE_SECRET = "BOT_TIMEZONE"
CATALOG_NOTIFY_SECRET = "CATALOG_NOTIFY"
CATALOG_VERSIONS_SECRET = "CATALOG_VERSIONS"

SETUP_PATH = "/setup"
DASHBOARD_PATH = "/dashboard"
LOGIN_PATH = "/login"

EXCLUDED_PATHS = {
    "/health", "/health/live", "/health/ready",
    "/login", "/logout", SETUP_PATH,
    "/forgot-password", "/reset-password",
    "/static/",
}

# ── Rate limiter ────────────────────────────────────────────────────────────

class _SimpleRateLimiter:
    def __init__(self):
        self._hits: dict[str, list[float]] = {}

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        self._hits.setdefault(key, [])
        self._hits[key] = [t for t in self._hits[key] if now - t < window_seconds]
        if len(self._hits[key]) >= limit:
            return False
        self._hits[key].append(now)
        return True

_rate_limiter = _SimpleRateLimiter()

# ── Admin existence cache ───────────────────────────────────────────────────

_admin_exists_cache = TTLCache(maxsize=1, ttl=20)

# ── Integration status cache ────────────────────────────────────────────────

_integration_status_cache = TTLCache(maxsize=1, ttl=30)

# ── Time helpers ─────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)

# ── Login helpers ───────────────────────────────────────────────────────────

_ALLOWED_LOGINS_RAW = (os.getenv("ADMIN_LOGINS") or "").strip()

def _login_allowed(login: str) -> bool:
    if not _ALLOWED_LOGINS_RAW:
        return True
    return login in (l.strip() for l in _ALLOWED_LOGINS_RAW.split(","))

_GENERIC_LOGIN_ERROR = "Неверный логин или пароль"

def _generic_login_error() -> str:
    return _GENERIC_LOGIN_ERROR

def _normalize_login(raw: str) -> str:
    return (raw or "").strip().lower()

def _login_format_ok(login: str) -> tuple[bool, str | None]:
    if not login:
        return False, "Логин обязателен"
    if len(login) < 3:
        return False, "Логин должен содержать минимум 3 символа"
    if len(login) > 64:
        return False, "Логин не должен превышать 64 символа"
    return True, None

# ── CSRF helpers ─────────────────────────────────────────────────────────────

def _ensure_csrf(request: Request) -> tuple[str, bool]:
    token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if token:
        return token, False
    token = secrets.token_urlsafe(32)
    return token, True

def _verify_csrf(request: Request, token: str) -> None:
    from fastapi import HTTPException
    cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not token or token != cookie:
        raise HTTPException(403, "Неверный CSRF-токен")

# ── Client IP ────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"

# ── Has admin ────────────────────────────────────────────────────────────────

async def _has_admin(session, use_cache: bool = True) -> bool:
    if use_cache:
        cached = _admin_exists_cache.get("flag")
        if cached is not None:
            return bool(cached)
    from sqlalchemy import select
    from database.models import BotAppUser
    r = await session.execute(
        select(BotAppUser.id).where(BotAppUser.role == "admin").limit(1)
    )
    has = r.scalar_one_or_none() is not None
    if use_cache:
        _admin_exists_cache["flag"] = has
    return has

# ── Events log ───────────────────────────────────────────────────────────────

def _append_ops_to_events_log(line: str) -> None:
    events_log_path = (os.getenv("ADMIN_EVENTS_LOG_PATH") or "").strip()
    if not events_log_path:
        return
    try:
        p = Path(events_log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[ADMIN] {_now_utc().isoformat()} {line}\n")
    except Exception as e:
        logger.warning("events_log write error: %s", e)


def _append_audit_file_line(message: str) -> None:
    audit_log_path = (os.getenv("ADMIN_AUDIT_LOG_PATH") or "").strip()
    if not audit_log_path:
        return
    try:
        p = Path(audit_log_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{_now_utc().isoformat()} {message}\n")
    except Exception as e:
        logger.warning("audit_log write error: %s", e)

# ── Secret masking ───────────────────────────────────────────────────────────

def _mask_secret(value: str | None, mask_url: bool = False) -> str:
    if not value:
        return ""
    if mask_url:
        return value
    if len(value) <= 4:
        return "•" * len(value)
    return value[:2] + "•" * (len(value) - 4) + value[-2:]

# ── Catalog parsing ──────────────────────────────────────────────────────────

def _parse_catalog_payload(notify_json: str, versions_json: str) -> tuple[list[str], list[str]]:
    import json
    try:
        notify = json.loads(notify_json) if notify_json else []
    except (json.JSONDecodeError, TypeError):
        notify = []
    try:
        versions = json.loads(versions_json) if versions_json else []
    except (json.JSONDecodeError, TypeError):
        versions = []
    if not isinstance(notify, list):
        notify = []
    if not isinstance(versions, list):
        versions = []
    notify = [str(x).strip() for x in notify if str(x).strip()]
    versions = [str(x).strip() for x in versions if str(x).strip()]
    return notify, versions
