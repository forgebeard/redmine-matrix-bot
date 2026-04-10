"""
Веб-админка: пользователи бота и маршруты Matrix (Postgres).

Запуск: uvicorn admin_main:app --host 0.0.0.0 --port 8080
Требуется DATABASE_URL (доступ к UI — через логин и пароль).
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from html import escape as html_escape
import logging
import os
import re
import sys
import secrets
import threading
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, available_timezones
from jinja2 import Environment, FileSystemLoader

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    AppSecret,
    BotHeartbeat,
    BotOpsAudit,
    BotAppUser,
    BotSession,
    BotUser,
    GroupVersionRoute,
    PasswordResetToken,
    StatusRoomRoute,
    SupportGroup,
    UserVersionRoute,
    VersionRoomRoute,
)
from database.session import get_session, get_session_factory
from mail import mask_identifier
from security import (
    SecurityError,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    load_master_key,
    make_reset_token,
    token_hash,
    validate_password_policy,
    verify_password,
)

from admin.crud_events_log import (
    actor_label_for_crud_log,
    format_crud_line,
    sanitize_audit_details,
    want_admin_audit_crud_db,
    want_admin_events_log_crud,
)
from dash_service_display import service_card_context
from events_log_display import (
    admin_events_log_timestamp_now,
    events_log_to_csv_bytes,
    filter_parsed_lines_by_local_date,
    parse_events_log_for_table,
    parse_ui_date_param,
)
from ops.docker_control import DockerControlError, control_service, get_service_status
from ui_datetime import bot_display_timezone, format_datetime_ui

# Jinja2 окружение и шаблоны теперь в helpers.py — импортируем чтобы не было дубликатов
# и чтобы фильтры (dt_ui) работали во всех роутах.
from admin.helpers import _jinja_env, templates


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    # Fail-fast: without master key we cannot safely work with encrypted secrets.
    try:
        load_master_key()
    except SecurityError as e:
        raise RuntimeError(f"startup failed: {e}") from e
    # Service timezone can be configured in onboarding and persisted as secret.
    try:
        factory = get_session_factory()
        async with factory() as session:
            tz_saved = await _load_secret_plain(session, SERVICE_TIMEZONE_SECRET)
        os.environ["BOT_TIMEZONE"] = _normalize_service_timezone_name(tz_saved)
    except Exception:
        logger.warning("service_timezone_load_failed", exc_info=True)
    yield


app = FastAPI(title="Matrix bot control panel", version="0.1.0", lifespan=_app_lifespan)

_STATIC_ROOT = _ROOT / "static"
if _STATIC_ROOT.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_ROOT)), name="static")


def _admin_csp_value() -> str | None:
    """
    Content-Security-Policy для HTML-ответов.
    ADMIN_CSP_POLICY — полная строка политики (приоритет).
    ADMIN_ENABLE_CSP=1 — встроенная политика под текущие CDN (htmx, FA, Google Fonts)
    и inline script/style (обработчики в шаблонах до выноса в .js).
    """
    explicit = (os.getenv("ADMIN_CSP_POLICY") or "").strip()
    if explicit:
        return explicit
    if os.getenv("ADMIN_ENABLE_CSP", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    return (
        "default-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "connect-src 'self';"
    )


@app.middleware("http")
async def _csp_middleware(request: Request, call_next):
    response = await call_next(request)
    csp = _admin_csp_value()
    if csp:
        response.headers["Content-Security-Policy"] = csp
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response

SESSION_COOKIE_NAME = os.getenv("ADMIN_SESSION_COOKIE", "admin_session")
CSRF_COOKIE_NAME = os.getenv("ADMIN_CSRF_COOKIE", "admin_csrf")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0").strip().lower() in ("1", "true", "yes", "on")
SETUP_PATH = "/setup"
# Путь дашборда в адресной строке (корень `/` отдаёт тот же экран без редиректа).
DASHBOARD_PATH = "/dashboard"
SESSION_IDLE_TIMEOUT_SECONDS = int(os.getenv("ADMIN_SESSION_IDLE_TIMEOUT", "1800"))
RUNTIME_STATUS_FILE = os.getenv("BOT_RUNTIME_STATUS_FILE", "/app/data/runtime_status.json")
# Системная строка в support_groups (миграции); в UI не показываем как обычную группу.
GROUP_UNASSIGNED_NAME = "UNASSIGNED"
# Подпись в интерфейсе для пользователей без group_id и для фильтра «только без группы».
GROUP_UNASSIGNED_DISPLAY = "Без группы"
# Совпадает с подписью первой опции фильтра на /users — запись в support_groups с этим именем даёт дубль в select.
GROUP_USERS_FILTER_ALL_LABEL = "Все группы"

_jinja_env.globals["GROUP_UNASSIGNED_NAME"] = GROUP_UNASSIGNED_NAME
_jinja_env.globals["GROUP_UNASSIGNED_DISPLAY"] = GROUP_UNASSIGNED_DISPLAY
_jinja_env.globals["GROUP_USERS_FILTER_ALL_LABEL"] = GROUP_USERS_FILTER_ALL_LABEL
_jinja_env.globals["dashboard_path"] = DASHBOARD_PATH

AUTH_TOKEN_SALT = os.getenv("AUTH_TOKEN_SALT", "dev-token-salt")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
RESET_TOKEN_TTL_SECONDS = int(os.getenv("RESET_TOKEN_TTL_SECONDS", "1800"))
RESET_COOLDOWN_SECONDS = int(os.getenv("RESET_COOLDOWN_SECONDS", "90"))


@lru_cache(maxsize=1)
def _standard_timezone_options() -> list[str]:
    """IANA timezone list with RU priority zones first."""
    preferred = [
        "Europe/Moscow",
        "Asia/Ufa",
        "Asia/Yekaterinburg",
        "Asia/Omsk",
        "Asia/Krasnoyarsk",
        "Asia/Irkutsk",
        "Asia/Vladivostok",
    ]
    values = sorted(tz for tz in available_timezones() if "/" in tz and not tz.startswith(("Etc/", "posix/", "right/")))
    ordered = [tz for tz in preferred if tz in values]
    preferred_set = set(ordered)
    ordered.extend([tz for tz in values if tz not in preferred_set])
    return ordered


@lru_cache(maxsize=1)
def _top_timezone_options() -> list[str]:
    """Frequently used timezones for the default compact select list."""
    preferred = [
        "Europe/Moscow",
        "Europe/Kaliningrad",
        "Europe/Samara",
        "Europe/Volgograd",
        "Europe/Astrakhan",
        "Europe/Ulyanovsk",
        "Europe/Kirov",
        "Europe/Simferopol",
        "Europe/Minsk",
        "Europe/Kyiv",
        "Europe/Riga",
        "Europe/Vilnius",
        "Europe/Tallinn",
        "Europe/Warsaw",
        "Europe/Berlin",
        "Europe/Paris",
        "Europe/London",
        "Europe/Madrid",
        "Europe/Rome",
        "Europe/Istanbul",
        "Asia/Yerevan",
        "Asia/Tbilisi",
        "Asia/Baku",
        "Asia/Almaty",
        "Asia/Tashkent",
        "Asia/Yekaterinburg",
        "Asia/Ufa",
        "Asia/Omsk",
        "Asia/Novosibirsk",
        "Asia/Krasnoyarsk",
        "Asia/Vladivostok",
    ]
    all_set = set(_standard_timezone_options())
    result = [tz for tz in preferred if tz in all_set]
    if len(result) < 30:
        for tz in _standard_timezone_options():
            if tz in result:
                continue
            result.append(tz)
            if len(result) >= 30:
                break
    return result[:30]


def _timezone_labels(options: list[str]) -> dict[str, str]:
    """Readable timezone labels with UTC offset and local time."""
    labels: dict[str, str] = {}
    for tz_name in options:
        try:
            now_local = datetime.now(ZoneInfo(tz_name))
            delta = now_local.utcoffset() or timedelta(0)
            total_minutes = int(delta.total_seconds() // 60)
            sign = "+" if total_minutes >= 0 else "-"
            abs_minutes = abs(total_minutes)
            hh = abs_minutes // 60
            mm = abs_minutes % 60
            labels[tz_name] = f"{tz_name} (UTC{sign}{hh:02d}:{mm:02d}, {now_local:%H:%M})"
        except Exception:
            labels[tz_name] = tz_name
    return labels

APP_MASTER_KEY_FILE = os.getenv("APP_MASTER_KEY_FILE", "/run/secrets/app_master_key")
SHOW_DEV_TOKENS = os.getenv("SHOW_DEV_TOKENS", "0").strip().lower() in ("1", "true", "yes", "on")
ADMIN_EXISTS_CACHE_TTL_SECONDS = int(os.getenv("ADMIN_EXISTS_CACHE_TTL_SECONDS", "20"))
INTEGRATION_STATUS_CACHE_TTL_SECONDS = int(os.getenv("INTEGRATION_STATUS_CACHE_TTL_SECONDS", "30"))
REQUIRED_SECRET_NAMES = [
    v.strip()
    for v in os.getenv(
        "REQUIRED_SECRET_NAMES",
        "REDMINE_URL,REDMINE_API_KEY,MATRIX_HOMESERVER,MATRIX_ACCESS_TOKEN,MATRIX_USER_ID",
    ).split(",")
    if v.strip()
]
MATRIX_DEFAULT_DEVICE_ID = (os.getenv("MATRIX_DEFAULT_DEVICE_ID") or "redmine_bot").strip() or "redmine_bot"


def _mask_secret(value: str, mask_url: bool = False) -> str:
    """Маскирует секретное значение.
    
    Для URL и MXID — показываем полностью (mask_url=False по умолчанию).
    Для ключей/токенов — показываем первые 6 и последние 4 символа.
    """
    if not value:
        return ""
    if mask_url:
        return value
    if len(value) <= 12:
        return value[:4] + "••••"
    return value[:6] + "••••••••" + value[-4:]


def _matrix_bot_mxid() -> str:
    """MXID бота из .env — подсказка в «Мои настройки» (без отдельной страницы привязки)."""
    return (os.getenv("MATRIX_USER_ID") or "").strip()


async def _matrix_bot_mxid_from_db(session: AsyncSession) -> str:
    """Читает MXID бота из БД (для Zero-Config режима)."""
    return await _load_secret_plain(session, "MATRIX_USER_ID")


async def _matrix_domain_from_db(session: AsyncSession) -> str:
    """Извлекает домен из MXID бота, сохраненного в БД."""
    mxid = await _matrix_bot_mxid_from_db(session)
    if ":" in mxid:
        return mxid.split(":", 1)[1]
    return ""


def _matrix_domain() -> str:
    """Извлекает домен из MXID бота: @bot:messenger.red-soft.ru → messenger.red-soft.ru.
    (Fallback на env для обратной совместимости, но в Zero-Config читается из БД)."""
    mxid = _matrix_bot_mxid()
    if ":" in mxid:
        return mxid.split(":", 1)[1]
    return ""


async def _get_matrix_domain_from_db(session: AsyncSession) -> str:
    """Читает домен из БД (для использования в роутах)."""
    mxid = await _load_secret_plain(session, "MATRIX_USER_ID")
    if ":" in mxid:
        return mxid.split(":", 1)[1]
    return _matrix_domain()  # Fallback на env
NOTIFY_TYPE_KEYS: list[str] = []
CATALOG_NOTIFY_SECRET = "__catalog_notify"
CATALOG_VERSIONS_SECRET = "__catalog_versions"
SERVICE_TIMEZONE_SECRET = "__service_timezone"
SERVICE_TIMEZONE_FALLBACK = "Europe/Moscow"

ADMIN_BOOTSTRAP_FIRST_ADMIN = (os.getenv("ADMIN_BOOTSTRAP_FIRST_ADMIN", "0").strip().lower() in ("1", "true", "yes", "on"))

_LOGIN_RE = re.compile(r"^[a-zA-Z0-9@._+-]{3,255}$")


def _normalize_service_timezone_name(value: str) -> str:
    tz_name = (value or "").strip()
    if tz_name and tz_name in set(_standard_timezone_options()):
        return tz_name
    return SERVICE_TIMEZONE_FALLBACK


def _admin_allowlist() -> frozenset[str]:
    raw = (os.getenv("ADMIN_LOGINS") or "").strip()
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def _normalize_login(raw: str) -> str:
    return (raw or "").strip().lower()


def _login_allowed(login: str) -> bool:
    allow = _admin_allowlist()
    if not allow:
        return True
    return login in allow


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _generic_login_error() -> str:
    return "Неверный логин или пароль"


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _admin_events_log_path() -> Path:
    raw = (os.getenv("ADMIN_EVENTS_LOG_PATH") or "").strip()
    if raw:
        return Path(raw)
    return _ROOT / "data" / "bot.log"


def _read_log_tail(path: Path, *, max_lines: int = 400, max_bytes: int = 256_000) -> str:
    try:
        if not path.is_file():
            return (
                f"Файл лога не найден: {path}\n"
                "Проверьте LOG_TO_FILE у бота, том data/ и переменную ADMIN_EVENTS_LOG_PATH."
            )
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read().decode("utf-8", errors="replace")
        lines = chunk.splitlines()
        return "\n".join(lines[-max_lines:]) if lines else ""
    except OSError as e:
        return f"Не удалось прочитать лог: {e}"


def _admin_audit_log_path() -> Path | None:
    raw = (os.getenv("ADMIN_AUDIT_LOG_PATH") or "").strip()
    if raw.lower() in ("-", "none", "off", "false", "0"):
        return None
    if not raw:
        return _ROOT / "data" / "admin_audit.log"
    p = Path(raw)
    return p if p.is_absolute() else _ROOT / p


def _append_audit_file_line(message: str) -> None:
    path = _admin_audit_log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = admin_events_log_timestamp_now()
        line = f"{ts} [AUDIT] {(message or '').strip()}\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        logger.warning("Не удалось записать файл аудита (%s): %s", path, e)


def _admin_events_log_scan_bytes() -> int:
    raw = (os.getenv("ADMIN_EVENTS_LOG_SCAN_BYTES") or str(8 * 1024 * 1024)).strip()
    try:
        n = int(raw)
    except ValueError:
        return 8 * 1024 * 1024
    return max(64 * 1024, min(n, 64 * 1024 * 1024))


def _read_events_log_scan(path: Path, *, max_bytes: int) -> tuple[str, bool]:
    """
    Читает файл событий целиком или хвост (если больше max_bytes).
    Возвращает (текст, truncated): при усечении первая строка может быть обрезана и отбрасывается.
    """
    try:
        if not path.is_file():
            return (
                f"Файл лога не найден: {path}\n"
                "Проверьте LOG_TO_FILE у бота, том data/ и переменную ADMIN_EVENTS_LOG_PATH.",
            ), False
        size = path.stat().st_size
        with path.open("rb") as f:
            if size <= max_bytes:
                data = f.read()
                truncated = False
            else:
                f.seek(max(0, size - max_bytes))
                data = f.read()
                truncated = True
        text = data.decode("utf-8", errors="replace")
        if truncated:
            nl = text.find("\n")
            if nl != -1 and nl + 1 < len(text):
                text = text[nl + 1 :]
        return text, truncated
    except OSError as e:
        return f"Не удалось прочитать лог: {e}", False


def _append_ops_to_events_log(message: str) -> None:
    """
    Дублирует операции Docker из панели в файл «Событий» (по умолчанию data/bot.log),
    чтобы страница /events показывала то же, что видит админ в UI (лог бота при этом не заменяется).
    """
    path: Path | None = None
    try:
        path = _admin_events_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = admin_events_log_timestamp_now()
        safe = (message or "").replace("\n", " ").replace("\r", " ").strip()[:800]
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{ts} [ADMIN] {safe}\n")
    except OSError as e:
        logger.warning(
            "Не удалось дописать строку [ADMIN] в лог событий %s: %s",
            path or "(unknown)",
            e,
            exc_info=True,
        )


def _dash_events_tail_line_count(*, max_lines: int = 400) -> int:
    """Число непустых строк в хвосте лога событий (как на /events), без учёта отсутствующего файла."""
    path = _admin_events_log_path()
    if not path.is_file():
        return 0
    text = _read_log_tail(path, max_lines=max_lines)
    return sum(1 for line in text.splitlines() if line.strip())


async def _dashboard_counts(session: AsyncSession) -> dict[str, int]:
    user_count = int(
        (await session.execute(select(func.count()).select_from(BotUser))).scalar_one() or 0
    )
    group_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(SupportGroup)
                .where(SupportGroup.name != GROUP_UNASSIGNED_NAME)
            )
        ).scalar_one()
        or 0
    )
    users_ungrouped = int(
        (
            await session.execute(
                select(func.count())
                .select_from(BotUser)
                .where(
                    or_(
                        BotUser.group_id.is_(None),
                        BotUser.group_id.in_(
                            select(SupportGroup.id).where(SupportGroup.name == GROUP_UNASSIGNED_NAME)
                        ),
                    )
                )
            )
        ).scalar_one()
        or 0
    )
    return {
        "user_count": user_count,
        "group_count": group_count,
        "users_without_group": users_ungrouped,
        "events_tail_lines": _dash_events_tail_line_count(),
    }


def _parse_status_keys_list(raw: str) -> list[str]:
    parts = [p.strip() for p in (raw or "").replace("\n", ",").split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _parse_json_string_list(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _default_notify_catalog() -> list[dict[str, str]]:
    return []


def _default_versions_catalog() -> list[str]:
    return []


def _catalog_key_from_label(label: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    if not base:
        base = "opt"
    key = base
    i = 2
    while key in used:
        key = f"{base}_{i}"
        i += 1
    return key


def _normalize_notify_catalog(data) -> list[dict[str, str]]:
    if not isinstance(data, list):
        return _default_notify_catalog()
    out: list[dict[str, str]] = []
    used: set[str] = set()
    for item in data:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            key = str(item.get("key") or "").strip().lower()
        else:
            label = str(item).strip()
            key = ""
        if not label:
            continue
        if not key:
            key = _catalog_key_from_label(label, used)
        if key in used:
            continue
        used.add(key)
        out.append({"key": key, "label": label})
    return out


def _normalize_versions_catalog(data) -> list[str]:
    if not isinstance(data, list):
        return _default_versions_catalog()
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


async def _load_secret_plain(session: AsyncSession, name: str) -> str:
    q = await session.execute(select(AppSecret).where(AppSecret.name == name))
    row = q.scalar_one_or_none()
    if row is None:
        return ""
    key = load_master_key()
    try:
        return decrypt_secret(row.ciphertext, row.nonce, key)
    except SecurityError:
        logger.warning("secret_decrypt_failed name=%s", name)
        return ""


async def _upsert_secret_plain(session: AsyncSession, name: str, value: str) -> None:
    key = load_master_key()
    enc = encrypt_secret(value, key=key)
    q = await session.execute(select(AppSecret).where(AppSecret.name == name))
    row = q.scalar_one_or_none()
    if row is None:
        session.add(
            AppSecret(name=name, ciphertext=enc.ciphertext, nonce=enc.nonce, key_version=enc.key_version)
        )
        return
    row.ciphertext = enc.ciphertext
    row.nonce = enc.nonce
    row.key_version = enc.key_version


async def _load_catalogs(session: AsyncSession) -> tuple[list[dict[str, str]], list[str]]:
    raw_notify = await _load_secret_plain(session, CATALOG_NOTIFY_SECRET)
    raw_versions = await _load_secret_plain(session, CATALOG_VERSIONS_SECRET)
    if raw_notify:
        try:
            notify_catalog = _normalize_notify_catalog(json.loads(raw_notify))
        except json.JSONDecodeError:
            notify_catalog = _default_notify_catalog()
    else:
        notify_catalog = _default_notify_catalog()
    if raw_versions:
        try:
            versions_catalog = _normalize_versions_catalog(json.loads(raw_versions))
        except json.JSONDecodeError:
            versions_catalog = _default_versions_catalog()
    else:
        versions_catalog = _default_versions_catalog()
    return notify_catalog, versions_catalog


def _parse_catalog_payload(notify_raw: str, versions_raw: str) -> tuple[list[dict[str, str]], list[str]]:
    if notify_raw:
        try:
            notify_catalog = _normalize_notify_catalog(json.loads(notify_raw))
        except json.JSONDecodeError:
            notify_catalog = _default_notify_catalog()
    else:
        notify_catalog = _default_notify_catalog()
    if versions_raw:
        try:
            versions_catalog = _normalize_versions_catalog(json.loads(versions_raw))
        except json.JSONDecodeError:
            versions_catalog = _default_versions_catalog()
    else:
        versions_catalog = _default_versions_catalog()
    return notify_catalog, versions_catalog


def _normalized_group_filter_key(name: str) -> str:
    """Нормализация имени для сравнения с подписью фильтра (без дублей «Все группы» в select)."""
    normalized = unicodedata.normalize("NFKC", name or "")
    compact_spaces = " ".join(normalized.replace("\u00a0", " ").split())
    return compact_spaces.strip().casefold()


def _group_excluded_from_assignable_lists(name: str | None) -> bool:
    if name is None:
        return False
    s = str(name).strip()
    if not s:
        return False
    if s == GROUP_UNASSIGNED_NAME:
        return True
    if _normalized_group_filter_key(s) == _normalized_group_filter_key(GROUP_USERS_FILTER_ALL_LABEL):
        return True
    return False


def _groups_assignable(groups: list) -> list:
    return [g for g in groups if not _group_excluded_from_assignable_lists(getattr(g, "name", None))]


def _is_reserved_support_group(row) -> bool:
    return row is not None and getattr(row, "name", None) == GROUP_UNASSIGNED_NAME


def _group_display_name(groups_by_id: dict, group_id: int | None) -> str:
    if group_id is None:
        return GROUP_UNASSIGNED_DISPLAY
    g = groups_by_id.get(group_id)
    if not g:
        return GROUP_UNASSIGNED_DISPLAY
    if g.name == GROUP_UNASSIGNED_NAME:
        return GROUP_UNASSIGNED_DISPLAY
    return g.name


_OPS_FLASH_MESSAGES: dict[str, str] = {
    "stop_ok": "Остановка бота выполнена. Если контейнер уже был выключен, состояние не менялось.",
    "stop_error": "Не удалось остановить бот. Проверьте DOCKER_HOST, docker-socket-proxy и имя сервиса (DOCKER_TARGET_SERVICE, метки compose).",
    "start_ok": "Бот запущен. Если он уже работал, ничего не изменилось.",
    "start_error": "Не удалось запустить бот. Проверьте Docker и настройки.",
    "restart_accepted": "Перезапуск бота запланирован (команда уходит в фоне).",
    "ops_commit_error": "Не удалось сохранить запись в журнал операций (БД). Состояние Docker смотрите в выводе compose / на дашборде.",
}

_OPS_FLASH_WITH_DETAIL = frozenset({"stop_error", "start_error", "ops_commit_error"})


def _truncate_ops_detail(s: str, max_len: int = 400) -> str:
    t = (s or "").replace("\n", " ").replace("\r", " ")
    if len(t) > max_len:
        return t[: max_len - 1] + "…"
    return t


def _ops_flash_message(ops: str | None, detail: str | None = None) -> str | None:
    if not ops:
        return None
    key = ops.strip()
    base = _OPS_FLASH_MESSAGES.get(key)
    if not base:
        return None
    d = (detail or "").strip()
    if d and key in _OPS_FLASH_WITH_DETAIL:
        return f"{base} Подробнее: {d}"
    return base


def _ensure_csrf(request: Request) -> tuple[str, bool]:
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if token:
        return token, False
    return secrets.token_urlsafe(24), True


def _verify_csrf(request: Request, form_token: str = "") -> None:
    """Проверка double-submit CSRF: поле формы или заголовок X-CSRF-Token (для HTMX)."""
    token = (form_token or "").strip()
    if not token:
        token = request.headers.get("X-CSRF-Token", "").strip()
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not cookie_token or not token or token != cookie_token:
        raise HTTPException(status_code=400, detail="Некорректный CSRF токен")


def _verify_csrf_json(request: Request) -> None:
    """CSRF-проверка для JSON-endpoints (тестовое сообщение и т.п.)."""
    token = request.headers.get("X-CSRF-Token", "").strip()
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not cookie_token or not token or token != cookie_token:
        raise HTTPException(status_code=400, detail="Некорректный CSRF токен")


async def _audit_op(
    session: AsyncSession,
    action: str,
    status: str,
    actor_login: str | None = None,
    detail: str | None = None,
) -> None:
    row = BotOpsAudit(
        actor_login=(actor_login or "").strip().lower() or None,
        action=action,
        status=status,
        detail=(detail or "")[:2000] or None,
    )
    session.add(row)
    d = ((detail or "").replace("\n", " "))[:1800]
    parts = [f"op={action}", f"status={status}"]
    al = (actor_login or "").strip()
    if al:
        parts.append(f"actor={al}")
    if d:
        parts.append(f"detail={d}")
    _append_audit_file_line(" ".join(parts))
    logger.info(
        json.dumps(
            {
                "level": "AUDIT",
                "action": action,
                "status": status,
                "actor": actor_login or "",
                "detail": detail or "",
                "ts": _now_utc().isoformat(),
            },
            ensure_ascii=False,
        )
    )


class _SimpleRateLimiter:
    """In-memory rate limiter (per process)."""

    def __init__(self):
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        now = datetime.now().timestamp()
        q = self._buckets[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


_rate_limiter = _SimpleRateLimiter()
logger = logging.getLogger("admin")


def _infer_crud_entity_id(entity_type: str, details: dict | None) -> int | None:
    """Числовой идентификатор сущности для индексации в bot_ops_audit (эвристика по типу)."""
    if not details:
        return None

    def gint(v: object) -> int | None:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        try:
            return int(str(v).strip())
        except (ValueError, TypeError):
            return None

    et = (entity_type or "").strip()
    if et == "bot_user":
        return gint(details.get("id"))
    if et == "group":
        return gint(details.get("id"))
    if et in ("group_version_route", "group_status_route"):
        return gint(details.get("group_id"))
    if et == "user_version_route":
        return gint(details.get("bot_user_id"))
    if et == "route/version_global":
        return gint(details.get("id"))
    if et == "self_settings":
        return gint(details.get("bot_user_id"))
    return None


async def _persist_admin_crud_audit(
    session: AsyncSession,
    request_actor,
    entity_type: str,
    crud_action: str,
    details: dict | None,
) -> None:
    actor_login = (getattr(request_actor, "login", None) or "").strip().lower() or None
    cleaned = sanitize_audit_details(details or {})
    entity_id = _infer_crud_entity_id(entity_type, details)
    et = (entity_type or "unknown")[:64]
    ca = (crud_action or "unknown")[:32]
    dj = json.dumps(cleaned, ensure_ascii=False) if cleaned else ""
    if len(dj) > 2000:
        dj = dj[:1997] + "..."
    aud = f"ADMIN_CRUD entity={et} action={ca} actor={actor_login or ''}"
    if entity_id is not None:
        aud += f" entity_id={entity_id}"
    if dj:
        aud += f" details={dj}"
    _append_audit_file_line(aud)
    logger.info(
        json.dumps(
            {
                "level": "AUDIT",
                "action": "ADMIN_CRUD",
                "status": "ok",
                "actor": actor_login or "",
                "entity_type": et,
                "crud_action": ca,
                "entity_id": entity_id,
                "details": cleaned,
                "ts": _now_utc().isoformat(),
            },
            ensure_ascii=False,
        )
    )
    if not want_admin_audit_crud_db():
        return
    row = BotOpsAudit(
        actor_login=actor_login,
        action="ADMIN_CRUD",
        status="ok",
        detail=None,
        entity_type=et or None,
        entity_id=entity_id,
        crud_action=ca or None,
        details_json=cleaned if cleaned else None,
    )
    session.add(row)


async def _maybe_log_admin_crud(
    session: AsyncSession,
    request_actor,
    entity_type: str,
    action: str,
    details: dict | None = None,
) -> None:
    if want_admin_events_log_crud():
        actor = actor_label_for_crud_log(request_actor)
        line = format_crud_line(entity_type, action, actor, details)
        _append_ops_to_events_log(line)
    await _persist_admin_crud_audit(session, request_actor, entity_type, action, details)


# Кэши и хелперы теперь в helpers.py — импортируем чтобы не было дубликатов
from admin.helpers import (
    _admin_exists_cache,
    _integration_status_cache,
    _has_admin,
    _ensure_csrf,
    _verify_csrf,
    _normalize_login,
    _login_format_ok,
    _login_allowed,
    _generic_login_error,
    _client_ip,
    _rate_limiter,
    _now_utc,
    _append_ops_to_events_log,
    _append_audit_file_line,
    _mask_secret,
    _parse_catalog_payload,
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    COOKIE_SECURE,
    AUTH_TOKEN_SALT,
    SETUP_PATH,
    DASHBOARD_PATH,
    templates,
)


class _RedmineSearchBreaker:
    """In-memory circuit breaker для поиска пользователей Redmine."""

    def __init__(self):
        self.failures = 0
        self.cooldown_until_ts = 0.0

    def blocked(self) -> bool:
        return datetime.now().timestamp() < self.cooldown_until_ts

    def on_success(self) -> None:
        self.failures = 0
        self.cooldown_until_ts = 0.0

    def on_failure(self) -> None:
        self.failures += 1
        if self.failures >= 5:
            self.cooldown_until_ts = datetime.now().timestamp() + 60


_redmine_search_breaker = _RedmineSearchBreaker()


def _runtime_status_from_file() -> dict:
    p = Path(RUNTIME_STATUS_FILE)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


async def _integration_status(session: AsyncSession, use_cache: bool = True) -> dict:
    if use_cache:
        cached = _integration_status_cache.get("flag")
        if cached is not None:
            return cached
    rows = await session.execute(select(AppSecret.name).where(AppSecret.name.in_(REQUIRED_SECRET_NAMES)))
    names = {r[0] for r in rows.all()}
    missing = [name for name in REQUIRED_SECRET_NAMES if name not in names]
    status = {
        "configured": len(missing) == 0,
        "missing": missing,
    }
    _integration_status_cache["flag"] = status
    return status


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Auth для админки через DB-сессии после входа по логину и паролю.
    """

    async def dispatch(self, request: Request, call_next):
        p = request.url.path
        if p.startswith("/static/") or p == "/favicon.ico":
            return await call_next(request)
        if p in (
            "/login",
            "/forgot-password",
            "/reset-password",
            "/health",
            "/health/live",
            "/health/ready",
            SETUP_PATH,
        ) or p.startswith("/docs") or p in (
            "/openapi.json",
            "/redoc",
        ):
            return await call_next(request)

        try:
            factory = get_session_factory()
            async with factory() as session:
                has_admin = await _has_admin(session)
        except Exception:
            # Если БД недоступна/не настроена, не падаем на middleware для публичных редиректов.
            return RedirectResponse("/login", status_code=303)

        if not has_admin and p != SETUP_PATH:
            return RedirectResponse(SETUP_PATH, status_code=303)

        token_raw = request.cookies.get(SESSION_COOKIE_NAME, "")
        if not token_raw:
            return RedirectResponse("/login", status_code=303)

        try:
            token_uuid = uuid.UUID(token_raw)
        except Exception:
            return RedirectResponse("/login", status_code=303)

        factory = get_session_factory()
        try:
            async with factory() as session:
                now = _now_utc()
                s = await session.execute(
                    select(BotSession).where(
                        BotSession.session_token == token_uuid,
                        BotSession.expires_at > now,
                    )
                )
                sess = s.scalar_one_or_none()
                if not sess:
                    return RedirectResponse("/login", status_code=303)

                u = await session.execute(
                    select(BotAppUser).where(BotAppUser.id == sess.user_id)
                )
                user = u.scalar_one_or_none()
                if not user:
                    return RedirectResponse("/login", status_code=303)
                if sess.session_version != getattr(user, "session_version", 1):
                    return RedirectResponse("/login", status_code=303)

                # Sliding idle timeout: продлеваем активную сессию на каждый запрос.
                sess.expires_at = now + timedelta(seconds=SESSION_IDLE_TIMEOUT_SECONDS)
                await session.flush()
                await session.commit()

                request.state.current_user = user
                request.state.integration_status = await _integration_status(session)
        except Exception:
            return RedirectResponse("/login", status_code=303)

        csrf_token, set_csrf_cookie = _ensure_csrf(request)
        request.state.csrf_token = csrf_token
        response = await call_next(request)
        if set_csrf_cookie:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                csrf_token,
                httponly=True,
                secure=COOKIE_SECURE,
                samesite="lax",
                path="/",
            )
        return response

app.add_middleware(AuthMiddleware)

# ═══════════════════════════════════════════════════════════════════════════
# ROUTERS
# ═══════════════════════════════════════════════════════════════════════════

from admin.routes.health import router as health_router
from admin.routes.auth import router as auth_router
from admin.routes.ops import router as ops_router
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(ops_router)


# ═══════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════


async def _dashboard_page(request: Request, session: AsyncSession):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    runtime_file = _runtime_status_from_file()
    try:
        runtime_docker = get_service_status()
    except DockerControlError as e:
        runtime_docker = {
            "state": "error",
            "detail": str(e),
            "service": os.getenv("DOCKER_TARGET_SERVICE", "bot"),
            "container_name": "",
            "docker_status": "",
            "started_at": "",
            "running": False,
        }
    tz = (os.getenv("BOT_TIMEZONE") or "Europe/Moscow").strip()
    service_ctx = service_card_context(runtime_docker, runtime_file, tz)
    dash = await _dashboard_counts(session)
    integration_status = await _integration_status(session)
    ops_flash = _ops_flash_message(
        request.query_params.get("ops"),
        request.query_params.get("ops_detail"),
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "runtime_status": {"cycle": runtime_file},
            "service_ctx": service_ctx,
            "dash": dash,
            "integration_status": integration_status,
            "ops_flash": ops_flash,
        },
    )


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    return await _dashboard_page(request, session)


@app.get(DASHBOARD_PATH, response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    return await _dashboard_page(request, session)


@app.get("/dash/service-strip", response_class=HTMLResponse)
async def dash_service_strip(request: Request):
    """Фрагмент карточки «Сервис» (HTMX poll): Docker + runtime_status.json."""
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    runtime_file = _runtime_status_from_file()
    try:
        runtime_docker = get_service_status()
    except DockerControlError as e:
        runtime_docker = {
            "state": "error",
            "detail": str(e),
            "service": os.getenv("DOCKER_TARGET_SERVICE", "bot"),
            "container_name": "",
            "docker_status": "",
            "started_at": "",
            "running": False,
        }
    tz = (os.getenv("BOT_TIMEZONE") or "Europe/Moscow").strip()
    ctx = service_card_context(runtime_docker, runtime_file, tz)
    html = _jinja_env.get_template("partials/service_metrics.html").render(service_ctx=ctx)
    return HTMLResponse(html)


def _restart_in_background(actor_login: str | None) -> None:
    def _run() -> None:
        time.sleep(1.5)
        detail = ""
        status = "ok"
        try:
            control_service("restart")
            detail = "restart command accepted"
        except Exception as e:  # noqa: BLE001
            status = "error"
            detail = str(e)

        async def _persist() -> None:
            factory = get_session_factory()
            async with factory() as s:
                await _audit_op(s, "BOT_RESTART", status, actor_login=actor_login, detail=detail)
                await s.commit()

        try:
            asyncio.run(_persist())
        except Exception:
            logger.exception("failed to persist restart audit")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ═══════════════════════════════════════════════════════════════════════════
# SECRETS / APP-USERS
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/secrets", response_class=HTMLResponse)
async def secrets_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    rows = await session.execute(select(AppSecret).order_by(AppSecret.name))
    items = list(rows.scalars().all())
    csrf_token, set_cookie = _ensure_csrf(request)
    resp = templates.TemplateResponse(
        request,
        "secrets.html",
        {"items": items, "error": None, "csrf_token": csrf_token},
    )
    if set_cookie:
        resp.set_cookie(CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return resp


@app.post("/secrets")
async def secrets_save(
    request: Request,
    name: Annotated[str, Form()],
    value: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    name = (name or "").strip()
    value = (value or "").strip()
    if not name or not value:
        raise HTTPException(400, "Имя и значение обязательны")
    key = load_master_key()
    enc = encrypt_secret(value, key=key)
    r = await session.execute(select(AppSecret).where(AppSecret.name == name))
    row = r.scalar_one_or_none()
    if row is None:
        row = AppSecret(name=name, ciphertext=enc.ciphertext, nonce=enc.nonce, key_version=enc.key_version)
        session.add(row)
    else:
        row.ciphertext = enc.ciphertext
        row.nonce = enc.nonce
        row.key_version = enc.key_version
    _integration_status_cache.invalidate()
    logger.info(
        "secret_updated name=%s actor=%s key_version=%s",
        name,
        mask_identifier(user.login),
        enc.key_version,
    )
    return RedirectResponse("/secrets", status_code=303)


@app.get("/app-users", response_class=HTMLResponse)
async def app_users_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    rows = await session.execute(select(BotAppUser).order_by(BotAppUser.login))
    users = list(rows.scalars().all())
    csrf_token, set_cookie = _ensure_csrf(request)
    resp = templates.TemplateResponse(
        request,
        "app_users.html",
        {"users": users, "csrf_token": csrf_token},
    )
    if set_cookie:
        resp.set_cookie(CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return resp


@app.post("/app-users/{user_id}/reset-password-admin")
async def app_user_reset_password_admin(
    request: Request,
    user_id: str,
    new_password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    current = getattr(request.state, "current_user", None)
    if not current or getattr(current, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    uid = uuid.UUID(user_id)
    q = await session.execute(select(BotAppUser).where(BotAppUser.id == uid))
    target = q.scalar_one_or_none()
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    ok, reason = validate_password_policy(new_password, login=target.login)
    if not ok:
        rows = await session.execute(select(BotAppUser).order_by(BotAppUser.login))
        users = list(rows.scalars().all())
        csrf_out, set_cookie = _ensure_csrf(request)
        resp = templates.TemplateResponse(
            request,
            "app_users.html",
            {
                "users": users,
                "csrf_token": csrf_out,
                "password_reset_error": reason or "Пароль не соответствует требованиям",
                "password_reset_login": target.login,
            },
        )
        if set_cookie:
            resp.set_cookie(CSRF_COOKIE_NAME, csrf_out, httponly=True, secure=COOKIE_SECURE, samesite="lax")
        return resp
    target.password_hash = hash_password(new_password)
    target.session_version = (target.session_version or 1) + 1
    await session.execute(delete(BotSession).where(BotSession.user_id == target.id))
    logger.info(
        "admin_password_reset target=%s actor=%s",
        mask_identifier(target.login),
        mask_identifier(current.login),
    )
    return RedirectResponse("/app-users", status_code=303)


@app.post("/app-users/{user_id}/change-login-admin")
async def app_user_change_login_admin(
    request: Request,
    user_id: str,
    new_login: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    current = getattr(request.state, "current_user", None)
    if not current or getattr(current, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    uid = uuid.UUID(user_id)
    q = await session.execute(select(BotAppUser).where(BotAppUser.id == uid))
    target = q.scalar_one_or_none()
    if not target:
        raise HTTPException(404, "Пользователь не найден")

    async def _err(msg: str):
        rows = await session.execute(select(BotAppUser).order_by(BotAppUser.login))
        users = list(rows.scalars().all())
        csrf_out, set_cookie = _ensure_csrf(request)
        resp = templates.TemplateResponse(
            request,
            "app_users.html",
            {
                "users": users,
                "csrf_token": csrf_out,
                "login_change_error": msg,
                "login_change_old_login": target.login,
            },
        )
        if set_cookie:
            resp.set_cookie(CSRF_COOKIE_NAME, csrf_out, httponly=True, secure=COOKIE_SECURE, samesite="lax")
        return resp

    new_login_n = _normalize_login(new_login)
    fmt_ok, fmt_err = _login_format_ok(new_login_n)
    if not fmt_ok:
        return await _err(fmt_err or "Некорректный логин")
    if not _login_allowed(new_login_n):
        return await _err("Этот логин не разрешён (проверьте ADMIN_LOGINS в окружении).")
    if new_login_n == target.login:
        return RedirectResponse("/app-users", status_code=303)
    taken = await session.execute(
        select(BotAppUser.id).where(BotAppUser.login == new_login_n, BotAppUser.id != uid).limit(1)
    )
    if taken.scalar_one_or_none() is not None:
        return await _err("Логин уже занят.")

    old_login = target.login
    target.login = new_login_n
    await session.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == target.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(requested_login=new_login_n)
    )
    await session.commit()
    logger.info(
        "admin_login_changed old=%s new=%s actor=%s",
        mask_identifier(old_login),
        mask_identifier(new_login_n),
        mask_identifier(current.login),
    )
    return RedirectResponse("/app-users", status_code=303)


# --- Пользователи ---


@app.get("/groups", response_class=HTMLResponse)
async def groups_list(
    request: Request,
    q: str = "",
    highlight_group_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    q = (q or "").strip()
    stmt = select(SupportGroup)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(SupportGroup.name.ilike(like), SupportGroup.room_id.ilike(like)))
    stmt = stmt.order_by(SupportGroup.is_active.desc(), SupportGroup.name.asc())
    _all_groups = list((await session.execute(stmt)).scalars().all())
    rows = [r for r in _all_groups if r.name != GROUP_UNASSIGNED_NAME]
    return templates.TemplateResponse(
        request,
        "groups_list.html",
        {
            "items": rows,
            "q": q,
            "highlight_group_id": highlight_group_id,
            "list_total": len(rows),
        },
    )


@app.get("/groups/new", response_class=HTMLResponse)
async def groups_new(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    notify_catalog, versions_catalog = await _load_catalogs(session)
    return templates.TemplateResponse(
        request,
        "group_form.html",
        {
            "title": "Новая группа",
            "g": None,
            "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "timezone_top_options": _top_timezone_options(),
            "timezone_all_options": _standard_timezone_options(),
            "timezone_labels": _timezone_labels(_standard_timezone_options()),
            "status_routes": [],
            "status_err": "",
            "status_msg": "",
            "notify_json": '["all"]',
            "notify_preset": "all",
            "notify_selected": ["all"],
            "notify_catalog": notify_catalog,
            "versions_catalog": versions_catalog,
            "initial_version_keys": "",
            "selected_version_keys": [],
            "version_preset": "all",
        },
    )


@app.post("/groups/test-message")
async def group_test_message(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Отправляет тестовое сообщение в комнату группы по room_id."""
    try:
        _verify_csrf_json(request)
    except HTTPException as e:
        return JSONResponse({"ok": False, "error": "Ошибка CSRF токена"}, status_code=e.status_code)

    admin_user = getattr(request.state, "current_user", None)
    if not admin_user or getattr(admin_user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    # Получаем готовый клиент
    client = await _get_matrix_client(session)
    if not client:
        return JSONResponse({"ok": False, "error": "Matrix не настроен"}, status_code=400)

    try:
        form = await request.form()
        room_id = (form.get("room_id") or "").strip()
    except Exception as e:
        logger.error("Failed to parse form: %s", e)
        await client.close()
        return JSONResponse({"ok": False, "error": "Не удалось прочитать данные формы"}, status_code=400)

    if not room_id:
        await client.close()
        return JSONResponse({"ok": False, "error": "Не указан ID комнаты"}, status_code=400)

    # Формируем сообщение
    from datetime import datetime as _dt
    from src.matrix_send import room_send_with_retry

    ts = _dt.now().strftime("%H:%M:%S")
    html = (
        f"<b>🧪 Тестовое сообщение группы</b><br>"
        f"Это тест от панели управления.<br>"
        f"Если вы это видите — подключение работает!<br>"
        f"<small>Отправлено: {ts}</small>"
    )
    text_plain = f"🧪 Тестовое сообщение группы\nЭто тест от панели управления.\nОтправлено: {ts}"

    try:
        # Синхронизируемся, чтобы получить список комнат
        logger.info("group_test_message: syncing to find room %s...", room_id)
        if not await _sync_matrix_client(client):
            await client.close()
            return JSONResponse({"ok": False, "error": "Не удалось синхронизироваться с Matrix"}, status_code=500)

        if room_id not in client.rooms:
            await client.close()
            return JSONResponse({"ok": False, "error": f"Бот не является участником комнаты {room_id}. Пригласите его в Matrix."}, status_code=400)

        logger.info("group_test_message: sending to %s", room_id)
        content = {"msgtype": "m.text", "body": text_plain, "format": "org.matrix.custom.html", "formatted_body": html}
        await room_send_with_retry(client, room_id, content)
        await client.close()
        return JSONResponse({"ok": True})
    except Exception as e:
        import traceback as _tb
        logger.error("group_test_message_failed room_id=%s\n%s", room_id, _tb.format_exc())
        await client.close()
        return JSONResponse({"ok": False, "error": "Не удалось отправить сообщение. Проверьте логи админки."}, status_code=500)


@app.get("/groups/{group_id}/edit", response_class=HTMLResponse)
async def groups_edit(
    request: Request,
    group_id: int,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row:
        raise HTTPException(404, "Группа не найдена")
    if _is_reserved_support_group(row):
        raise HTTPException(404, "Группа не найдена")
    status_err = (request.query_params.get("status_err") or "").strip()
    status_msg = (request.query_params.get("status_msg") or "").strip()
    version_err = (request.query_params.get("version_err") or "").strip()
    version_msg = (request.query_params.get("version_msg") or "").strip()
    room = (row.room_id or "").strip()
    sr_stmt = select(StatusRoomRoute).where(StatusRoomRoute.room_id == room).order_by(StatusRoomRoute.status_key)
    status_rows = list((await session.execute(sr_stmt)).scalars().all()) if room else []
    gv_stmt = (
        select(GroupVersionRoute)
        .where(GroupVersionRoute.group_id == group_id)
        .order_by(GroupVersionRoute.version_key)
    )
    version_rows = list((await session.execute(gv_stmt)).scalars().all())
    notify_catalog, versions_catalog = await _load_catalogs(session)
    notify_keys = {item["key"] for item in notify_catalog}
    notify_selected = [str(x).strip() for x in (row.notify or ["all"]) if str(x).strip()]
    if "all" not in notify_selected:
        notify_selected = [k for k in notify_selected if k in notify_keys]
    version_set = set(versions_catalog)
    selected_versions = [r.version_key for r in version_rows if r.version_key in version_set]
    return templates.TemplateResponse(
        request,
        "group_form.html",
        {
            "title": "Редактирование группы",
            "g": row,
            "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "timezone_top_options": _top_timezone_options(),
            "timezone_all_options": _standard_timezone_options(),
            "timezone_labels": _timezone_labels(_standard_timezone_options()),
            "status_routes": status_rows,
            "status_err": status_err,
            "status_msg": status_msg,
            "version_routes": version_rows,
            "version_err": version_err,
            "version_msg": version_msg,
            "notify_json": json.dumps(row.notify, ensure_ascii=False),
            "notify_preset": _notify_preset(row.notify),
            "notify_selected": notify_selected,
            "notify_catalog": notify_catalog,
            "versions_catalog": versions_catalog,
            "selected_version_keys": selected_versions,
            "version_preset": _version_preset(selected_versions, versions_catalog),
        },
    )


@app.post("/groups")
async def groups_create(
    request: Request,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str | None, Form()] = None,
    initial_status_keys: Annotated[str, Form()] = "",
    initial_version_keys: Annotated[str, Form()] = "",
    version_keys_json: Annotated[str, Form()] = "",
    version_preset: Annotated[str, Form()] = "all",
    version_values: Annotated[list[str], Form()] = [],
    notify_json: Annotated[str, Form()] = "",
    notify_preset: Annotated[str, Form()] = "all",
    notify_values: Annotated[list[str], Form()] = [],
    work_hours: Annotated[str, Form()] = "",
    work_hours_from: Annotated[str, Form()] = "",
    work_hours_to: Annotated[str, Form()] = "",
    work_days_json: Annotated[str, Form()] = "",
    work_days_values: Annotated[list[str], Form()] = [],
    dnd: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    notify_catalog, versions_catalog = await _load_catalogs(session)
    notify_allowed = [item["key"] for item in notify_catalog]
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    n = (name or "").strip()
    if not n:
        raise HTTPException(400, "Название обязательно")
    if n == GROUP_UNASSIGNED_NAME:
        raise HTTPException(400, "Это имя зарезервировано для системы")
    if _normalized_group_filter_key(n) == _normalized_group_filter_key(GROUP_USERS_FILTER_ALL_LABEL):
        raise HTTPException(400, "Это имя зарезервировано для фильтра списка пользователей")
    existing_name = await session.execute(
        select(SupportGroup.id).where(SupportGroup.name == n).limit(1)
    )
    if existing_name.scalar_one_or_none() is not None:
        raise HTTPException(400, "Группа с таким названием уже существует")
    room = (room_id or "").strip()
    if not room:
        raise HTTPException(400, "Укажите ID комнаты группы")
    status_keys = _parse_status_keys_list(initial_status_keys)
    if work_hours_from and work_hours_to:
        wh = f"{work_hours_from.strip()}-{work_hours_to.strip()}"
    else:
        wh = work_hours.strip() or None
    if work_days_values:
        wd = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        wd = _parse_work_days(work_days_json)
    if notify_preset == "all":
        notify = ["all"]
    elif notify_preset == "new_only":
        notify = ["new"]
    elif notify_preset == "overdue_only":
        notify = ["overdue"]
    elif notify_preset == "custom":
        notify = _normalize_notify(notify_values, notify_allowed)
    else:
        notify = _parse_notify(notify_json)
    row = SupportGroup(
        name=n,
        room_id=room,
        timezone=(timezone_name or "").strip() or None,
        # Group form no longer exposes this switch; default new groups to active.
        is_active=True if is_active is None else is_active in ("1", "on", "true"),
        notify=notify,
        work_hours=wh,
        work_days=wd,
        dnd=dnd in ("1", "on", "true"),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(400, "Не удалось создать группу: проверьте уникальность названия")
    rid = row.id
    for key in status_keys:
        ex = await session.execute(select(StatusRoomRoute.id).where(StatusRoomRoute.status_key == key))
        if ex.scalar_one_or_none():
            continue
        session.add(StatusRoomRoute(status_key=key, room_id=room))
    version_keys = _parse_json_string_list(version_keys_json) or _parse_status_keys_list(initial_version_keys)
    if version_preset == "all":
        version_keys = list(versions_catalog)
    elif version_preset == "custom":
        version_keys = _normalize_versions(version_values, versions_catalog)
    for vkey in version_keys:
        ex = await session.execute(
            select(GroupVersionRoute.id).where(
                GroupVersionRoute.group_id == rid,
                GroupVersionRoute.version_key == vkey,
            )
        )
        if ex.scalar_one_or_none():
            continue
        session.add(GroupVersionRoute(group_id=rid, version_key=vkey, room_id=room))
    await _maybe_log_admin_crud(
        session,
        user,
        "group",
        "create",
        {"id": rid, "name": n},
    )
    return RedirectResponse(f"/groups?highlight_group_id={rid}", status_code=303)


@app.post("/groups/{group_id}")
async def groups_update(
    request: Request,
    group_id: int,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str | None, Form()] = None,
    notify_json: Annotated[str, Form()] = "",
    notify_preset: Annotated[str, Form()] = "all",
    notify_values: Annotated[list[str], Form()] = [],
    version_preset: Annotated[str, Form()] = "all",
    version_values: Annotated[list[str], Form()] = [],
    version_keys_json: Annotated[str, Form()] = "",
    initial_version_keys: Annotated[str, Form()] = "",
    work_hours: Annotated[str, Form()] = "",
    work_hours_from: Annotated[str, Form()] = "",
    work_hours_to: Annotated[str, Form()] = "",
    work_days_json: Annotated[str, Form()] = "",
    work_days_values: Annotated[list[str], Form()] = [],
    dnd: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    notify_catalog, versions_catalog = await _load_catalogs(session)
    notify_allowed = [item["key"] for item in notify_catalog]
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row:
        raise HTTPException(404, "Группа не найдена")
    if _is_reserved_support_group(row):
        raise HTTPException(403, "Системную группу нельзя менять")
    n = (name or "").strip()
    if not n:
        raise HTTPException(400, "Название обязательно")
    if n == GROUP_UNASSIGNED_NAME:
        raise HTTPException(400, "Это имя зарезервировано для системы")
    if _normalized_group_filter_key(n) == _normalized_group_filter_key(GROUP_USERS_FILTER_ALL_LABEL):
        raise HTTPException(400, "Это имя зарезервировано для фильтра списка пользователей")
    existing_name = await session.execute(
        select(SupportGroup.id).where(SupportGroup.name == n, SupportGroup.id != group_id).limit(1)
    )
    if existing_name.scalar_one_or_none() is not None:
        raise HTTPException(400, "Группа с таким названием уже существует")
    if work_hours_from and work_hours_to:
        wh = f"{work_hours_from.strip()}-{work_hours_to.strip()}"
    else:
        wh = work_hours.strip() or None
    if work_days_values:
        wd = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        wd = _parse_work_days(work_days_json)
    if notify_preset == "all":
        notify = ["all"]
    elif notify_preset == "new_only":
        notify = ["new"]
    elif notify_preset == "overdue_only":
        notify = ["overdue"]
    elif notify_preset == "custom":
        notify = _normalize_notify(notify_values, notify_allowed)
    else:
        notify = _parse_notify(notify_json)
    old_room = (row.room_id or "").strip()
    new_room = (room_id or "").strip()
    row.name = n
    row.room_id = new_room
    row.timezone = (timezone_name or "").strip() or None
    # Preserve existing value when control is absent in form payload.
    if is_active is not None:
        row.is_active = is_active in ("1", "on", "true")
    row.notify = notify
    row.work_hours = wh
    row.work_days = wd
    row.dnd = dnd in ("1", "on", "true")
    if version_preset == "all":
        submitted_versions = list(versions_catalog)
    elif version_preset == "custom":
        submitted_versions = _normalize_versions(version_values, versions_catalog)
    else:
        submitted_versions = _parse_json_string_list(version_keys_json) or _parse_status_keys_list(initial_version_keys)
    existing_routes = list(
        (
            await session.execute(
                select(GroupVersionRoute).where(GroupVersionRoute.group_id == group_id)
            )
        ).scalars().all()
    )
    existing_by_key = {r.version_key: r for r in existing_routes}
    submitted_set = set(submitted_versions)
    for r in existing_routes:
        if r.version_key not in submitted_set:
            await session.delete(r)
    for key in submitted_versions:
        ex = existing_by_key.get(key)
        if ex:
            ex.room_id = new_room
            continue
        session.add(GroupVersionRoute(group_id=group_id, version_key=key, room_id=new_room))
    if old_room and new_room and old_room != new_room:
        await session.execute(
            update(StatusRoomRoute).where(StatusRoomRoute.room_id == old_room).values(room_id=new_room)
        )
        await session.execute(
            update(GroupVersionRoute)
            .where(GroupVersionRoute.group_id == group_id, GroupVersionRoute.room_id == old_room)
            .values(room_id=new_room)
        )
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(400, "Не удалось сохранить группу: проверьте уникальность названия")
    await _maybe_log_admin_crud(
        session,
        user,
        "group",
        "update",
        {"id": group_id, "name": n},
    )
    return RedirectResponse(f"/groups?highlight_group_id={group_id}", status_code=303)


@app.post("/groups/{group_id}/status-routes/add")
async def group_status_route_add(
    request: Request,
    group_id: int,
    status_key: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row or _is_reserved_support_group(row):
        raise HTTPException(404, "Группа не найдена")
    room = (row.room_id or "").strip()
    if not room:
        return RedirectResponse(f"/groups/{group_id}/edit?status_err=no_room", status_code=303)
    key = (status_key or "").strip()
    if not key:
        return RedirectResponse(f"/groups/{group_id}/edit?status_err=empty", status_code=303)
    exists = await session.execute(select(StatusRoomRoute).where(StatusRoomRoute.status_key == key))
    if exists.scalar_one_or_none():
        return RedirectResponse(f"/groups/{group_id}/edit?status_err=exists", status_code=303)
    session.add(StatusRoomRoute(status_key=key, room_id=room))
    await _maybe_log_admin_crud(
        session,
        user,
        "group_status_route",
        "create",
        {"group_id": group_id, "status_key": key},
    )
    return RedirectResponse(f"/groups/{group_id}/edit?status_msg=added", status_code=303)


@app.post("/groups/{group_id}/status-routes/{route_row_id}/delete")
async def group_status_route_delete(
    request: Request,
    group_id: int,
    route_row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row or _is_reserved_support_group(row):
        raise HTTPException(404, "Группа не найдена")
    room = (row.room_id or "").strip()
    rte = await session.get(StatusRoomRoute, route_row_id)
    if not rte or (rte.room_id or "").strip() != room:
        raise HTTPException(404, "Маршрут не найден")
    sk = rte.status_key
    await session.delete(rte)
    await _maybe_log_admin_crud(
        session,
        user,
        "group_status_route",
        "delete",
        {"group_id": group_id, "status_key": sk, "route_id": route_row_id},
    )
    return RedirectResponse(f"/groups/{group_id}/edit?status_msg=deleted", status_code=303)


@app.post("/groups/{group_id}/version-routes/add")
async def group_version_route_add(
    request: Request,
    group_id: int,
    version_key: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row or _is_reserved_support_group(row):
        raise HTTPException(404, "Группа не найдена")
    room = (row.room_id or "").strip()
    if not room:
        return RedirectResponse(f"/groups/{group_id}/edit?version_err=no_room", status_code=303)
    key = (version_key or "").strip()
    if not key:
        return RedirectResponse(f"/groups/{group_id}/edit?version_err=empty", status_code=303)
    exists = await session.execute(
        select(GroupVersionRoute.id).where(
            GroupVersionRoute.group_id == group_id,
            GroupVersionRoute.version_key == key,
        )
    )
    if exists.scalar_one_or_none():
        return RedirectResponse(f"/groups/{group_id}/edit?version_err=exists", status_code=303)
    session.add(GroupVersionRoute(group_id=group_id, version_key=key, room_id=room))
    await _maybe_log_admin_crud(
        session,
        user,
        "group_version_route",
        "create",
        {"group_id": group_id, "version_key": key},
    )
    return RedirectResponse(f"/groups/{group_id}/edit?version_msg=added", status_code=303)


@app.post("/groups/{group_id}/version-routes/{route_row_id}/delete")
async def group_version_route_delete(
    request: Request,
    group_id: int,
    route_row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row or _is_reserved_support_group(row):
        raise HTTPException(404, "Группа не найдена")
    rte = await session.get(GroupVersionRoute, route_row_id)
    if not rte or rte.group_id != group_id:
        raise HTTPException(404, "Маршрут не найден")
    vkey = rte.version_key
    await session.delete(rte)
    await _maybe_log_admin_crud(
        session,
        user,
        "group_version_route",
        "delete",
        {"group_id": group_id, "version_key": vkey, "route_id": route_row_id},
    )
    return RedirectResponse(f"/groups/{group_id}/edit?version_msg=deleted", status_code=303)


@app.post("/groups/{group_id}/delete")
async def groups_delete(
    request: Request,
    group_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if row:
        if _is_reserved_support_group(row):
            raise HTTPException(403, "Системную группу нельзя удалить")
        gid, gname = row.id, row.name
        await session.delete(row)
        await _maybe_log_admin_crud(session, user, "group", "delete", {"id": gid, "name": gname})
    return RedirectResponse("/groups", status_code=303)


@app.get("/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    q: str = "",
    group_id: int | None = None,
    highlight_user_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    groups_rows = list((await session.execute(select(SupportGroup).order_by(SupportGroup.name.asc()))).scalars().all())
    groups_by_id = {g.id: g for g in groups_rows}

    stmt = select(BotUser)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                BotUser.display_name.ilike(like),
                BotUser.department.ilike(like),
                BotUser.room.ilike(like),
            )
        )
    if group_id is not None:
        if group_id == -1:
            stmt = stmt.where(BotUser.group_id.is_(None))
        else:
            stmt = stmt.where(BotUser.group_id == group_id)
    stmt = stmt.order_by(BotUser.group_id.asc().nulls_last(), BotUser.display_name.asc().nulls_last(), BotUser.redmine_id)
    rows = list((await session.execute(stmt)).scalars().all())

    grouped: dict[str, list[BotUser]] = {}
    for row in rows:
        key = _group_display_name(groups_by_id, row.group_id)
        grouped.setdefault(key, []).append(row)

    return templates.TemplateResponse(
        request,
        "users_list.html",
        {
            "users": rows,
            "grouped_users": grouped,
            "groups": _groups_assignable(groups_rows),
            "groups_by_id": groups_by_id,
            "q": q,
            "group_filter": group_id,
            "highlight_user_id": highlight_user_id,
            "list_total": len(rows),
        },
    )


@app.get("/users/new", response_class=HTMLResponse)
async def users_new(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    groups_rows = list((await session.execute(select(SupportGroup).order_by(SupportGroup.name.asc()))).scalars().all())
    notify_catalog, versions_catalog = await _load_catalogs(session)
    matrix_domain = await _get_matrix_domain_from_db(session)
    return templates.TemplateResponse(
        request,
        "user_form.html",
        {
            "title": "Новый пользователь",
            "u": None,
            "room_localpart": "",
            "matrix_domain": matrix_domain,
            "notify_json": '["all"]',
            "notify_preset": "all",
            "notify_selected": ["all"],
            "groups": _groups_assignable(groups_rows),
            "group_unassigned_display": GROUP_UNASSIGNED_DISPLAY,
            "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "timezone_top_options": _top_timezone_options(),
            "timezone_all_options": _standard_timezone_options(),
            "timezone_labels": _timezone_labels(_standard_timezone_options()),
            "notify_catalog": notify_catalog,
            "versions_catalog": versions_catalog,
            "selected_version_keys": [],
            "version_preset": "all",
        },
    )


def _parse_notify(raw: str) -> list:
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else ["all"]
    except json.JSONDecodeError:
        return ["all"]


def _normalize_notify(values: list[str] | None, allowed_keys: list[str] | None = None) -> list[str]:
    vals = [v.strip() for v in (values or []) if v and v.strip()]
    if not vals:
        return ["all"]
    if "all" in vals:
        return ["all"]
    allowed_set = set(allowed_keys or NOTIFY_TYPE_KEYS)
    allowed = [v for v in vals if v in allowed_set]
    return allowed or ["all"]


def _notify_preset(notify: list | None) -> str:
    values = [str(x).strip() for x in (notify or []) if str(x).strip()]
    if not values or "all" in values:
        return "all"
    return "custom"


def _parse_work_days(raw: str) -> list[int] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else None
    except json.JSONDecodeError:
        return None


def _parse_work_hours_range(value: str) -> tuple[str, str]:
    if not value or "-" not in value:
        return "", ""
    start, end = value.split("-", 1)
    return start.strip(), end.strip()


def _normalize_versions(values: list[str] | None, allowed_values: list[str] | None = None) -> list[str]:
    vals = [v.strip() for v in (values or []) if v and v.strip()]
    if not vals:
        return []
    allowed_set = set((allowed_values or []))
    if not allowed_set:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in vals:
        if v in seen or v not in allowed_set:
            continue
        seen.add(v)
        out.append(v)
    return out


def _version_preset(selected: list[str] | None, catalog: list[str] | None) -> str:
    selected_list = [str(x).strip() for x in (selected or []) if str(x).strip()]
    if not selected_list:
        return "all"
    # Keep manual mode after save: any explicit selection is treated as "custom".
    return "custom"


@app.post("/users")
async def users_create(
    request: Request,
    redmine_id: Annotated[int, Form()],
    room: Annotated[str, Form()],
    display_name: Annotated[str, Form()] = "",
    group_id: Annotated[str, Form()] = "",
    notify_json: Annotated[str, Form()] = "",
    notify_preset: Annotated[str, Form()] = "all",
    notify_values: Annotated[list[str], Form()] = [],
    initial_version_keys: Annotated[str, Form()] = "",
    version_keys_json: Annotated[str, Form()] = "",
    version_preset: Annotated[str, Form()] = "all",
    version_values: Annotated[list[str], Form()] = [],
    timezone_name: Annotated[str, Form()] = "",
    work_hours: Annotated[str, Form()] = "",
    work_hours_from: Annotated[str, Form()] = "",
    work_hours_to: Annotated[str, Form()] = "",
    work_days_json: Annotated[str, Form()] = "",
    work_days_values: Annotated[list[str], Form()] = [],
    dnd: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    notify_catalog, versions_catalog = await _load_catalogs(session)
    notify_allowed = [item["key"] for item in notify_catalog]
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    if work_hours_from and work_hours_to:
        wh = f"{work_hours_from.strip()}-{work_hours_to.strip()}"
    else:
        wh = work_hours.strip() or None
    if work_days_values:
        wd = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        wd = _parse_work_days(work_days_json)
    if notify_preset == "all":
        notify = ["all"]
    elif notify_preset == "new_only":
        notify = ["new"]
    elif notify_preset == "overdue_only":
        notify = ["overdue"]
    elif notify_preset == "custom":
        notify = _normalize_notify(notify_values, notify_allowed)
    else:
        notify = _parse_notify(notify_json)
    # Конструируем полный room_id из localpart + домен бота (асинхронно из БД)
    full_room = await _build_room_id_async(room.strip(), session)
    row = BotUser(
        redmine_id=redmine_id,
        display_name=display_name.strip() or None,
        group_id=int(group_id) if str(group_id).isdigit() else None,
        department=None,
        room=full_room,
        notify=notify,
        timezone=(timezone_name or "").strip() or None,
        work_hours=wh,
        work_days=wd,
        dnd=dnd in ("on", "true", "1"),
    )
    session.add(row)
    await session.flush()
    if version_preset == "all":
        version_keys = list(versions_catalog)
    elif version_preset == "custom":
        version_keys = _normalize_versions(version_values, versions_catalog)
    else:
        version_keys = _parse_json_string_list(version_keys_json) or _parse_status_keys_list(initial_version_keys)
    for vkey in version_keys:
        ex = await session.execute(
            select(UserVersionRoute.id).where(
                UserVersionRoute.bot_user_id == row.id,
                UserVersionRoute.version_key == vkey,
            )
        )
        if ex.scalar_one_or_none():
            continue
        session.add(UserVersionRoute(bot_user_id=row.id, version_key=vkey, room_id=row.room))
    await _maybe_log_admin_crud(
        session,
        user,
        "bot_user",
        "create",
        {
            "id": row.id,
            "redmine_id": redmine_id,
            "group_id": row.group_id,
        },
    )
    return RedirectResponse(f"/users?highlight_user_id={row.id}", status_code=303)


# --- Вспомогательные функции для Matrix (DRY) ---


async def _get_matrix_client(session: AsyncSession) -> AsyncClient | None:
    """
    Создает и настраивает Matrix-клиент на основе секретов из БД.
    Возвращает None, если секреты не настроены.
    """
    homeserver = await _load_secret_plain(session, "MATRIX_HOMESERVER")
    access_token = await _load_secret_plain(session, "MATRIX_ACCESS_TOKEN")
    bot_mxid = await _load_secret_plain(session, "MATRIX_USER_ID")

    if not all([homeserver, access_token, bot_mxid]):
        return None

    from nio import AsyncClient

    client = AsyncClient(homeserver, bot_mxid)
    client.access_token = access_token
    client.device_id = "redmine_bot_admin"
    # restore_login - синхронный метод
    client.restore_login(bot_mxid, "redmine_bot_admin", access_token)
    return client


async def _sync_matrix_client(client: AsyncClient, timeout: int = 10000) -> bool:
    """Синхронизирует клиент. Возвращает True при успехе."""
    try:
        await client.sync(timeout=timeout)
        return True
    except Exception:
        return False


# --- Отправка тестового сообщения (универсальный: по user_id или по MXID) ---


@app.post("/users/test-message")
async def user_test_message(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Отправляет тестовое сообщение по user_id (из БД) или напрямую по MXID."""
    _verify_csrf_json(request)
    admin_user = getattr(request.state, "current_user", None)
    if not admin_user or getattr(admin_user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    # Получаем готовый клиент (или None, если не настроен)
    client = await _get_matrix_client(session)
    if not client:
        return JSONResponse({"ok": False, "error": "Matrix не настроен (нет homeserver/token/user_id)"}, status_code=400)

    # Загружаем остальные настройки (Redmine)
    redmine_url = await _load_secret_plain(session, "REDMINE_URL")
    redmine_key = await _load_secret_plain(session, "REDMINE_API_KEY")
    bot_mxid = await _load_secret_plain(session, "MATRIX_USER_ID")

    form = await request.form()
    raw_uid = form.get("user_id", "")
    raw_mxid = form.get("mxid", "")

    uid = 0
    if raw_uid:
        try:
            uid = int(raw_uid)
        except ValueError:
            uid = 0

    target_mxid = (raw_mxid or "").strip()
    room_id = None  # Инициализируем заранее, чтобы избежать UnboundLocalError

    # Извлекаем домен из homeserver (https://messenger.red-soft.ru → messenger.red-soft.ru)
    # client.homeserver уже содержит URL, но надежнее взять из настроек
    homeserver = client.homeserver
    matrix_domain = homeserver.replace("https://", "").replace("http://", "").rstrip("/")

    # Если MXID введён вручную (не полный), добавляем домен
    if target_mxid and ":" not in target_mxid:
        if not target_mxid.startswith("@"):
            target_mxid = f"@{target_mxid}"
        target_mxid = f"{target_mxid}:{matrix_domain}"

    # Если указан user_id — берём данные из БД
    if uid > 0:
        row = await session.get(BotUser, uid)
        if not row:
            await client.close()
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=404)
        
        raw_room = (row.room or "").strip()
        
        # Нормализация: если в БД записан localpart или MXID, обрабатываем это
        if raw_room.startswith("@"):
            # Это MXID (личка)
            if ":" not in raw_room and matrix_domain:
                target_mxid = f"{raw_room}:{matrix_domain}"
            else:
                target_mxid = raw_room
            room_id = None
        elif raw_room.startswith("!"):
            # Это полная комната
            room_id = raw_room
        elif raw_room:
            # Это localpart (напр. dmitry.merenkov) -> считаем, что это личка
            if matrix_domain:
                target_mxid = f"@{raw_room}:{matrix_domain}"
            else:
                target_mxid = f"@{raw_room}"
            room_id = None

        # Если MXID все ещё не указан — пробуем получить из Redmine
        if not target_mxid and not room_id and redmine_url and redmine_key and row.redmine_id:
            try:
                from urllib.request import Request, urlopen
                import json as _json3
                api_url = f"{redmine_url.rstrip('/')}/users/{row.redmine_id}.json"
                req = Request(api_url, headers={"X-Redmine-API-Key": redmine_key, "Accept": "application/json"})
                with urlopen(req, timeout=10) as resp:
                    rdata = _json3.loads(resp.read().decode())
                    login = rdata.get("user", {}).get("login", "")
                    if login:
                        # Извлекаем домен из MXID бота (@bot:domain → domain)
                        domain = bot_mxid.split(":", 1)[1] if ":" in bot_mxid else ""
                        target_mxid = f"@{login}:{domain}" if domain else None
            except Exception:
                pass

    if not target_mxid and not room_id:
        await client.close()
        return JSONResponse({"ok": False, "error": "Не указан Matrix ID пользователя"}, status_code=400)

    # Формируем сообщение
    from datetime import datetime as _dt
    from src.matrix_send import room_send_with_retry

    ts = _dt.now().strftime("%H:%M:%S")
    html = (
        f"<b>🧪 Тестовое сообщение</b><br>"
        f"Это тест от панели управления.<br>"
        f"Если вы это видите — подключение работает!<br>"
        f"<small>Отправлено: {ts}</small>"
    )
    text_plain = f"🧪 Тестовое сообщение\nЭто тест от панели управления.\nОтправлено: {ts}"

    final_room_id = room_id

    try:
        if not final_room_id and target_mxid:
            # Синхронизируемся, чтобы найти DM
            logger.info("test_message: syncing to find DM for %s", target_mxid)
            await _sync_matrix_client(client)

            # Ищем существующую DM
            for r_id, room_obj in client.rooms.items():
                member_ids = {m.user_id for m in room_obj.users.values()}
                if len(member_ids) == 2 and bot_mxid in member_ids and target_mxid in member_ids:
                    final_room_id = r_id
                    logger.info("test_message: found existing DM %s", r_id)
                    break
            # Создаём DM
            if not final_room_id:
                logger.info("test_message: creating DM with %s", target_mxid)
                resp_create = await client.room_create(
                    invite=[target_mxid],
                    is_direct=True,
                )
                if resp_create and hasattr(resp_create, "room_id"):
                    final_room_id = resp_create.room_id
                    logger.info("test_message: created DM %s, joining...", final_room_id)
                    await client.join(final_room_id)
                else:
                    err_detail = str(resp_create) if resp_create else "no response"
                    await client.close()
                    return JSONResponse({"ok": False, "error": f"Не удалось создать DM с {target_mxid}: {err_detail}"}, status_code=500)

        if not final_room_id:
            await client.close()
            return JSONResponse({"ok": False, "error": "Не удалось определить комнату"}, status_code=500)

        logger.info("test_message: sending to %s", final_room_id)
        content = {"msgtype": "m.text", "body": text_plain, "format": "org.matrix.custom.html", "formatted_body": html}
        await room_send_with_retry(client, final_room_id, content)
        await client.close()
        return JSONResponse({"ok": True})
    except Exception as e:
        import traceback as _tb
        logger.error("test_message_failed uid=%s mxid=%s error=%s\n%s", uid, target_mxid, e, _tb.format_exc())
        await client.close()
        return JSONResponse({"ok": False, "error": "Не удалось отправить сообщение. Проверьте логи админки."}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════════
# Bot Heartbeat API (мониторинг живучести)
# ═══════════════════════════════════════════════════════════════════════════


@app.post("/api/bot/heartbeat")
async def bot_heartbeat_post(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Бот вызывает этот endpoint раз в минуту, чтобы сообщить, что он жив."""
    try:
        data = await request.json()
        instance_id_str = data.get("instance_id")
        if not instance_id_str:
            return JSONResponse({"ok": False, "error": "instance_id required"}, status_code=400)

        import uuid
        instance_id = uuid.UUID(instance_id_str)

        # Upsert heartbeat
        stmt = select(BotHeartbeat).where(BotHeartbeat.instance_id == instance_id)
        result = await session.execute(stmt)
        hb = result.scalar_one_or_none()

        if hb:
            from datetime import datetime, timezone
            hb.last_seen = datetime.now(timezone.utc)
        else:
            from datetime import datetime, timezone
            session.add(BotHeartbeat(instance_id=instance_id, last_seen=datetime.now(timezone.utc)))

        await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error("heartbeat_post_failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/bot/status", response_class=JSONResponse)
async def bot_status_get(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Возвращает статус бота для дашборда."""
    try:
        from datetime import datetime, timezone, timedelta

        # Ищем самый свежий heartbeat
        stmt = select(BotHeartbeat).order_by(BotHeartbeat.last_seen.desc()).limit(1)
        result = await session.execute(stmt)
        hb = result.scalar_one_or_none()

        if not hb:
            return {"status": "unknown", "last_seen": None, "message": "Бот ещё не отправлял heartbeat"}

        now = datetime.now(timezone.utc)
        diff = (now - hb.last_seen).total_seconds()

        if diff < 120:  # Менее 2 минут
            status = "alive"
            message = f"Бот активен ({int(diff)} сек. назад)"
        elif diff < 600:  # Менее 10 минут
            status = "warning"
            message = f"Бот может быть завис ({int(diff)} сек. назад)"
        else:
            status = "dead"
            message = f"Бот не отвечает ({int(diff)} сек. назад)"

        return {
            "status": status,
            "last_seen": hb.last_seen.isoformat(),
            "message": message,
            "seconds_ago": int(diff),
        }
    except Exception as e:
        logger.error("bot_status_failed: %s", e)
        return {"status": "error", "message": str(e)}


# --- Redmine: поиск users по имени/логину ---


@app.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def users_edit(
    request: Request,
    user_id: int,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(BotUser, user_id)
    if not row:
        raise HTTPException(404)
    version_err = (request.query_params.get("version_err") or "").strip()
    version_msg = (request.query_params.get("version_msg") or "").strip()
    uv_stmt = (
        select(UserVersionRoute)
        .where(UserVersionRoute.bot_user_id == user_id)
        .order_by(UserVersionRoute.version_key)
    )
    version_rows = list((await session.execute(uv_stmt)).scalars().all())
    groups_rows = list((await session.execute(select(SupportGroup).order_by(SupportGroup.name.asc()))).scalars().all())
    notify_catalog, versions_catalog = await _load_catalogs(session)
    matrix_domain = await _get_matrix_domain_from_db(session)
    notify_keys = {item["key"] for item in notify_catalog}
    notify_selected = [str(x).strip() for x in (row.notify or ["all"]) if str(x).strip()]
    if "all" not in notify_selected:
        notify_selected = [k for k in notify_selected if k in notify_keys]
    version_set = set(versions_catalog)
    selected_versions = [r.version_key for r in version_rows if r.version_key in version_set]
    return templates.TemplateResponse(
        request,
        "user_form.html",
        {
            "title": f"Пользователь Redmine {row.redmine_id}",
            "u": row,
            "room_localpart": _room_localpart(row.room),
            "matrix_domain": matrix_domain,
            "notify_json": json.dumps(row.notify, ensure_ascii=False),
            "notify_preset": _notify_preset(row.notify),
            "notify_selected": notify_selected,
            "groups": _groups_assignable(groups_rows),
            "group_unassigned_display": GROUP_UNASSIGNED_DISPLAY,
            "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "timezone_top_options": _top_timezone_options(),
            "timezone_all_options": _standard_timezone_options(),
            "timezone_labels": _timezone_labels(_standard_timezone_options()),
            "version_routes": version_rows,
            "version_keys_text": "\n".join(r.version_key for r in version_rows),
            "version_err": version_err,
            "version_msg": version_msg,
            "notify_catalog": notify_catalog,
            "versions_catalog": versions_catalog,
            "selected_version_keys": selected_versions,
            "version_preset": _version_preset(selected_versions, versions_catalog),
        },
    )


@app.post("/users/{user_id}")
async def users_update(
    request: Request,
    user_id: int,
    redmine_id: Annotated[int, Form()],
    room: Annotated[str, Form()],
    display_name: Annotated[str, Form()] = "",
    group_id: Annotated[str, Form()] = "",
    notify_json: Annotated[str, Form()] = "",
    notify_preset: Annotated[str, Form()] = "all",
    notify_values: Annotated[list[str], Form()] = [],
    version_preset: Annotated[str, Form()] = "all",
    version_values: Annotated[list[str], Form()] = [],
    version_keys_text: Annotated[str, Form()] = "",
    version_keys_json: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    work_hours: Annotated[str, Form()] = "",
    work_hours_from: Annotated[str, Form()] = "",
    work_hours_to: Annotated[str, Form()] = "",
    work_days_json: Annotated[str, Form()] = "",
    work_days_values: Annotated[list[str], Form()] = [],
    dnd: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    notify_catalog, versions_catalog = await _load_catalogs(session)
    notify_allowed = [item["key"] for item in notify_catalog]
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(BotUser, user_id)
    if not row:
        raise HTTPException(404)
    old_room = (row.room or "").strip()
    new_room = await _build_room_id_async(room.strip(), session)
    row.redmine_id = redmine_id
    row.display_name = display_name.strip() or None
    row.group_id = int(group_id) if str(group_id).isdigit() else None
    row.room = new_room
    row.timezone = (timezone_name or "").strip() or None
    if notify_preset == "all":
        row.notify = ["all"]
    elif notify_preset == "new_only":
        row.notify = ["new"]
    elif notify_preset == "overdue_only":
        row.notify = ["overdue"]
    elif notify_preset == "custom":
        row.notify = _normalize_notify(notify_values, notify_allowed)
    else:
        row.notify = _parse_notify(notify_json)
    if work_hours_from and work_hours_to:
        row.work_hours = f"{work_hours_from.strip()}-{work_hours_to.strip()}"
    else:
        row.work_hours = work_hours.strip() or None
    if work_days_values:
        row.work_days = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        row.work_days = _parse_work_days(work_days_json)
    row.dnd = dnd in ("on", "true", "1")
    if version_preset == "all":
        submitted_versions = list(versions_catalog)
    elif version_preset == "custom":
        submitted_versions = _normalize_versions(version_values, versions_catalog)
    else:
        submitted_versions = _parse_json_string_list(version_keys_json) or _parse_status_keys_list(version_keys_text)
    existing_routes = list(
        (
            await session.execute(
                select(UserVersionRoute).where(UserVersionRoute.bot_user_id == user_id)
            )
        ).scalars().all()
    )
    existing_by_key = {r.version_key: r for r in existing_routes}
    submitted_set = set(submitted_versions)
    for r in existing_routes:
        if r.version_key not in submitted_set:
            await session.delete(r)
    for key in submitted_versions:
        ex = existing_by_key.get(key)
        if ex:
            ex.room_id = new_room
            continue
        session.add(UserVersionRoute(bot_user_id=user_id, version_key=key, room_id=new_room))
    if old_room and new_room and old_room != new_room:
        await session.execute(
            update(UserVersionRoute)
            .where(UserVersionRoute.bot_user_id == user_id, UserVersionRoute.room_id == old_room)
            .values(room_id=new_room)
        )
    await _maybe_log_admin_crud(
        session,
        user,
        "bot_user",
        "update",
        {"id": user_id, "redmine_id": redmine_id},
    )
    return RedirectResponse(f"/users?highlight_user_id={user_id}", status_code=303)


@app.post("/users/{user_id}/version-routes/add")
async def user_version_route_add(
    request: Request,
    user_id: int,
    version_key: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(BotUser, user_id)
    if not row:
        raise HTTPException(404)
    room = (row.room or "").strip()
    if not room:
        return RedirectResponse(f"/users/{user_id}/edit?version_err=no_room", status_code=303)
    key = (version_key or "").strip()
    if not key:
        return RedirectResponse(f"/users/{user_id}/edit?version_err=empty", status_code=303)
    exists = await session.execute(
        select(UserVersionRoute.id).where(
            UserVersionRoute.bot_user_id == user_id,
            UserVersionRoute.version_key == key,
        )
    )
    if exists.scalar_one_or_none():
        return RedirectResponse(f"/users/{user_id}/edit?version_err=exists", status_code=303)
    session.add(UserVersionRoute(bot_user_id=user_id, version_key=key, room_id=room))
    await _maybe_log_admin_crud(
        session,
        user,
        "user_version_route",
        "create",
        {"bot_user_id": user_id, "version_key": key},
    )
    return RedirectResponse(f"/users/{user_id}/edit?version_msg=added", status_code=303)


@app.post("/users/{user_id}/version-routes/{route_row_id}/delete")
async def user_version_route_delete(
    request: Request,
    user_id: int,
    route_row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    rte = await session.get(UserVersionRoute, route_row_id)
    if not rte or rte.bot_user_id != user_id:
        raise HTTPException(404, "Маршрут не найден")
    vkey = rte.version_key
    await session.delete(rte)
    await _maybe_log_admin_crud(
        session,
        user,
        "user_version_route",
        "delete",
        {"bot_user_id": user_id, "version_key": vkey, "route_id": route_row_id},
    )
    return RedirectResponse(f"/users/{user_id}/edit?version_msg=deleted", status_code=303)


@app.post("/users/{user_id}/delete")
async def users_delete(
    request: Request,
    user_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(BotUser, user_id)
    if row:
        uid, rmid = row.id, row.redmine_id
        await session.delete(row)
        await _maybe_log_admin_crud(session, user, "bot_user", "delete", {"id": uid, "redmine_id": rmid})
    return RedirectResponse("/users", status_code=303)


# --- Вспомогательные функции для Matrix room_id ---


def _room_localpart(room_id: str) -> str:
    """Извлекает localpart из room_id: !xxxxxx:server → xxxxxx"""
    if not room_id:
        return ""
    # room_id формат: !<opaque>:<domain>
    if room_id.startswith("!") and ":" in room_id:
        return room_id[1:].split(":", 1)[0]
    return room_id


async def _build_room_id_async(localpart: str, session: AsyncSession) -> str:
    """Конструирует полный room_id из localpart + домен бота (читая домен из БД)."""
    domain = await _matrix_domain_from_db(session)
    if not localpart or not domain:
        return localpart
    if localpart.startswith("!"):
        return localpart  # уже полный ID комнаты
    if localpart.startswith("@"):
        return f"{localpart.split(':', 1)[0]}:{domain}" if ":" not in localpart else localpart
    return f"!{localpart}:{domain}"





@app.get("/redmine/users/search", response_class=HTMLResponse)
async def redmine_users_search(
    request: Request,
    q: str = "",
    limit: int = 20,
):
    """
    Возвращает HTML-параметры <option> для автозаполнения редмине_id.

    Важно: endpoint может быть использован даже без доступной Redmine-конфигурации —
    тогда просто вернёт пустой ответ.
    """
    q = (q or "").strip()
    try:
        limit_i = int(limit)
    except ValueError:
        limit_i = 20
    limit_i = max(1, min(limit_i, 50))

    if not q:
        return HTMLResponse("")

    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    if _redmine_search_breaker.blocked():
        logger.warning("Redmine search blocked due to cooldown")
        return HTMLResponse('<option value="">Поиск временно недоступен (cooldown)</option>')

    redmine_url = await _load_secret_plain(session, "REDMINE_URL")
    redmine_key = await _load_secret_plain(session, "REDMINE_API_KEY")

    if not redmine_url or not redmine_key:
        return HTMLResponse('<option value="">Redmine не настроен (нет URL/API key)</option>')

    def _do_search() -> tuple[list[dict], str | None]:
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError

        params = urlencode({"name": q, "limit": str(limit_i)})
        url = f"{redmine_url.rstrip('/')}/users.json?{params}"
        req = Request(url, headers={"X-Redmine-API-Key": redmine_key})
        try:
            with urlopen(req, timeout=5.0) as r:
                payload = json.loads(r.read().decode("utf-8", errors="replace"))
            items = payload.get("users") if isinstance(payload, dict) else []
            return (items if isinstance(items, list) else [], None)
        except HTTPError as e:
            return [], f"http_{e.code}"
        except URLError:
            return [], "timeout"
        except Exception:
            return [], "error"

    users_raw, err = await asyncio.to_thread(_do_search)
    if err:
        _redmine_search_breaker.on_failure()
        return HTMLResponse(f'<option value="">Ошибка поиска: {html_escape(err)}</option>')
    _redmine_search_breaker.on_success()
    users = users_raw

    opts: list[str] = []
    for u in users:
        uid = (u or {}).get("id") if isinstance(u, dict) else None
        if uid is None:
            continue
        firstname = (u or {}).get("firstname", "") if isinstance(u, dict) else ""
        lastname = (u or {}).get("lastname", "") if isinstance(u, dict) else ""
        login = (u or {}).get("login", "") if isinstance(u, dict) else ""
        label = " ".join([s for s in (firstname, lastname) if s]).strip()
        if not label:
            label = login or str(uid)
        # value должен быть числом redmine_id
        opts.append(
            f'<option value="{int(uid)}" data-display-name="{html_escape(label)}">{html_escape(label)}'
            f'{(" (" + html_escape(login) + ")") if login else ""}</option>'
        )
    if not opts:
        return HTMLResponse('<option value="">Ничего не найдено</option>')
    return HTMLResponse("".join(opts))


def _fetch_redmine_user_by_id(redmine_user_id: int, redmine_url: str, redmine_key: str) -> tuple[dict | None, str | None]:
    """GET /users/:id.json → (user dict, None) или (None, error_code)."""
    if not redmine_url or not redmine_key:
        return None, "not_configured"
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    url = f"{redmine_url.rstrip('/')}/users/{redmine_user_id}.json"
    req = Request(url, headers={"X-Redmine-API-Key": redmine_key})
    try:
        with urlopen(req, timeout=5.0) as r:
            payload = json.loads(r.read().decode("utf-8", errors="replace"))
        u = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(u, dict):
            return None, "bad_response"
        return u, None
    except HTTPError as e:
        if e.code == 404:
            return None, "not_found"
        return None, f"http_{e.code}"
    except URLError:
        return None, "timeout"
    except Exception:
        return None, "error"


@app.get("/redmine/users/lookup")
async def redmine_user_lookup(request: Request, user_id: int, session: AsyncSession = Depends(get_session)):
    """
    JSON для формы пользователя: по числовому Redmine user id подставить отображаемое имя.
    """
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    if user_id < 1:
        return JSONResponse({"ok": False, "error": "invalid_id"}, status_code=400)
    if _redmine_search_breaker.blocked():
        return JSONResponse({"ok": False, "error": "cooldown"}, status_code=503)

    redmine_url = await _load_secret_plain(session, "REDMINE_URL")
    redmine_key = await _load_secret_plain(session, "REDMINE_API_KEY")

    raw, err = await asyncio.to_thread(_fetch_redmine_user_by_id, user_id, redmine_url, redmine_key)
    if err == "not_configured":
        return JSONResponse({"ok": False, "error": "not_configured"})
    if err == "not_found":
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    if err:
        _redmine_search_breaker.on_failure()
        return JSONResponse({"ok": False, "error": err}, status_code=502)
    _redmine_search_breaker.on_success()

    firstname = str(raw.get("firstname") or "").strip()
    lastname = str(raw.get("lastname") or "").strip()
    login = str(raw.get("login") or "").strip()
    label = " ".join(s for s in (firstname, lastname) if s).strip()
    if not label:
        label = login or str(user_id)
    return JSONResponse(
        {
            "ok": True,
            "redmine_id": user_id,
            "display_name": label,
            "login": login,
        }
    )


# --- Маршруты по статусу ---


@app.get("/routes/status")
async def routes_status_legacy_redirect():
    """Старый URL: маршруты статусов настраиваются в карточке группы."""
    return RedirectResponse("/groups", status_code=303)


@app.post("/routes/status")
async def routes_status_add(
    request: Request,
    status_key: Annotated[str, Form()],
    room_id: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    key = status_key.strip()
    room = room_id.strip()
    if not key or not room:
        return RedirectResponse("/groups", status_code=303)
    exists = await session.execute(select(StatusRoomRoute).where(StatusRoomRoute.status_key == key))
    if exists.scalar_one_or_none():
        return RedirectResponse("/groups", status_code=303)
    session.add(StatusRoomRoute(status_key=key, room_id=room))
    return RedirectResponse("/groups", status_code=303)


@app.post("/routes/status/by-room")
async def routes_status_add_by_room(
    request: Request,
    room_id: Annotated[str, Form()],
    status_keys: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    room = room_id.strip()
    raw_statuses = status_keys.strip()
    if not room or not raw_statuses:
        raise HTTPException(400, "Комната и статусы обязательны")
    parts = [p.strip() for p in raw_statuses.replace("\n", ",").split(",")]
    statuses = [p for p in parts if p]
    existing_q = await session.execute(select(StatusRoomRoute.status_key))
    existing = {s[0] for s in existing_q.all()}
    added = 0
    skipped = 0
    for key in statuses:
        if key in existing:
            skipped += 1
            continue
        session.add(StatusRoomRoute(status_key=key, room_id=room))
        existing.add(key)
        added += 1
    return RedirectResponse("/groups", status_code=303)


@app.post("/routes/status/{row_id}/delete")
async def routes_status_del(
    request: Request,
    row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    await session.execute(delete(StatusRoomRoute).where(StatusRoomRoute.id == row_id))
    return RedirectResponse("/groups", status_code=303)


# --- Маршруты по версии ---


@app.get("/routes/version", response_class=HTMLResponse)
async def routes_version(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    r = await session.execute(select(VersionRoomRoute).order_by(VersionRoomRoute.version_key))
    rows = list(r.scalars().all())
    return templates.TemplateResponse(
        request,
        "routes_version.html",
        {"rows": rows},
    )


@app.post("/routes/version")
async def routes_version_add(
    request: Request,
    version_key: Annotated[str, Form()],
    room_id: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    vr = VersionRoomRoute(version_key=version_key.strip(), room_id=room_id.strip())
    session.add(vr)
    await session.flush()
    await _maybe_log_admin_crud(
        session,
        user,
        "route/version_global",
        "create",
        {"id": vr.id, "version_key": vr.version_key},
    )
    return RedirectResponse("/routes/version", status_code=303)


@app.post("/routes/version/{row_id}/delete")
async def routes_version_del(
    request: Request,
    row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    vr = await session.get(VersionRoomRoute, row_id)
    vkey = vr.version_key if vr else ""
    await session.execute(delete(VersionRoomRoute).where(VersionRoomRoute.id == row_id))
    await _maybe_log_admin_crud(
        session,
        user,
        "route/version_global",
        "delete",
        {"id": row_id, "version_key": vkey},
    )
    return RedirectResponse("/routes/version", status_code=303)


# --- События (файл лога: таблица + CSV; аудит панели — отдельный файл ADMIN_AUDIT_LOG_PATH) ---


def _events_filter_query_dict(
    date_from: str,
    date_to: str,
    time_at: str,
    page_size: int,
) -> dict[str, str]:
    d: dict[str, str] = {"page_size": str(page_size)}
    if date_from.strip():
        d["date_from"] = date_from.strip()
    if date_to.strip():
        d["date_to"] = date_to.strip()
    if time_at.strip():
        d["time_at"] = time_at.strip()
    return d


def _normalize_time_filter(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d{2}:\d{2}", raw):
        return raw
    return ""


def _load_filtered_event_lines(date_from_s: str, date_to_s: str, time_at_s: str):
    path = _admin_events_log_path()
    raw, truncated = _read_events_log_scan(path, max_bytes=_admin_events_log_scan_bytes())
    parsed = parse_events_log_for_table(raw)
    tz = bot_display_timezone()
    df = parse_ui_date_param(date_from_s)
    d_to = parse_ui_date_param(date_to_s)
    if (
        len(parsed) == 1
        and parsed[0].sort_key is None
        and (
            "Файл лога не найден" in (parsed[0].message or "")
            or "Не удалось прочитать" in (parsed[0].message or "")
        )
    ):
        filtered = parsed
    else:
        filtered = filter_parsed_lines_by_local_date(parsed, df, d_to, tz)
    time_filter = _normalize_time_filter(time_at_s)
    if time_filter and filtered:
        filtered = [row for row in filtered if str(getattr(row, "time_ui", "") or "").startswith(time_filter)]
    return filtered, truncated, path


@app.get("/events", response_class=HTMLResponse)
async def events_page(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    time_at: str = "",
    page: int = 1,
    page_size: int = 50,
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    try:
        page_i = max(1, int(page))
    except (TypeError, ValueError):
        page_i = 1
    try:
        page_size_i = int(page_size)
    except (TypeError, ValueError):
        page_size_i = 50
    page_size_i = min(200, max(5, page_size_i))

    normalized_time = _normalize_time_filter(time_at)
    rows, truncated, _log_path = _load_filtered_event_lines(date_from, date_to, normalized_time)
    total = len(rows)
    total_pages = max(1, (total + page_size_i - 1) // page_size_i) if total > 0 else 1
    page_i = max(1, min(page_i, total_pages))
    offset = (page_i - 1) * page_size_i
    page_rows = rows[offset : offset + page_size_i]

    qdict = _events_filter_query_dict(date_from, date_to, normalized_time, page_size_i)
    qs_base = urlencode(qdict)
    events_filter_link_prefix = f"/events?{qs_base}&" if qs_base else "/events?"

    return templates.TemplateResponse(
        request,
        "events.html",
        {
            "rows": page_rows,
            "total": total,
            "page": page_i,
            "page_size": page_size_i,
            "total_pages": total_pages,
            "filter_date_from": date_from,
            "filter_date_to": date_to,
            "filter_time_at": normalized_time,
            "events_filter_link_prefix": events_filter_link_prefix,
            "export_qs": qs_base,
            "events_log_truncated": truncated,
        },
    )


@app.get("/audit")
async def audit_legacy_redirect(request: Request):
    """Старый URL: журнал перенесён на /events."""
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    q = request.url.query
    loc = f"/events?{q}" if q else "/events"
    return RedirectResponse(loc, status_code=303)


@app.get("/events/export.csv")
async def events_export_csv(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    time_at: str = "",
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    rows, _truncated, _path = _load_filtered_event_lines(date_from, date_to, _normalize_time_filter(time_at))
    body = events_log_to_csv_bytes(rows, max_rows=50_000)
    stamp = _now_utc().strftime("%Y%m%d")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="events_log_{stamp}.csv"'},
    )


@app.get("/audit/export.csv")
async def audit_export_legacy_redirect(request: Request):
    """Старый URL: выгрузка перенесена на /events/export.csv."""
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    q = request.url.query
    loc = f"/events/export.csv?{q}" if q else "/events/export.csv"
    return RedirectResponse(loc, status_code=303)


# --- User self-service: настройки ---


@app.get("/me/settings", response_class=HTMLResponse)
async def me_settings_get(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if getattr(user, "role", "") == "admin":
        return RedirectResponse(DASHBOARD_PATH, status_code=303)

    redmine_id = getattr(user, "redmine_id", None)
    notify_catalog, _versions_catalog = await _load_catalogs(session)
    csrf_token, set_cookie = _ensure_csrf(request)
    if redmine_id is None:
        resp = templates.TemplateResponse(
            request,
            "my_settings.html",
            {
                "room": None,
                "notify_json": '["all"]',
                "notify_preset": "all",
                "notify_selected": ["all"],
                "notify_catalog": notify_catalog,
                "work_hours": "",
                "work_hours_from": "",
                "work_hours_to": "",
                "timezone_name": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
                "timezone_top_options": _top_timezone_options(),
                "timezone_all_options": _standard_timezone_options(),
                "timezone_labels": _timezone_labels(_standard_timezone_options()),
                "work_days_json": "",
                "work_days_selected": [0, 1, 2, 3, 4],
                "dnd": False,
                "error": (
                    "Учётная запись в панели ещё не связана с Redmine. "
                    "Подписка на уведомления настраивается через бота в Matrix "
                    "или попросите администратора завести вас в разделе «Пользователи»."
                ),
                "matrix_bot_mxid": _matrix_bot_mxid(),
                "csrf_token": csrf_token,
            },
            status_code=400,
        )
        if set_cookie:
            resp.set_cookie(CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax")
        return resp

    r = await session.execute(select(BotUser).where(BotUser.redmine_id == redmine_id))
    bot_user = r.scalar_one_or_none()
    if not bot_user:
        raise HTTPException(404, "BotUser не найден")
    notify_selected = [str(x).strip() for x in (bot_user.notify or ["all"]) if str(x).strip()]
    notify_keys = {item["key"] for item in notify_catalog}
    if "all" not in notify_selected:
        notify_selected = [k for k in notify_selected if k in notify_keys]

    resp = templates.TemplateResponse(
        request,
        "my_settings.html",
        {
            "room": bot_user.room,
            "notify_json": json.dumps(bot_user.notify, ensure_ascii=False)
            if bot_user.notify is not None
            else '["all"]',
            "notify_preset": _notify_preset(bot_user.notify),
            "notify_selected": notify_selected,
            "notify_catalog": notify_catalog,
            "work_hours": bot_user.work_hours or "",
            "work_hours_from": _parse_work_hours_range(bot_user.work_hours or "")[0],
            "work_hours_to": _parse_work_hours_range(bot_user.work_hours or "")[1],
            "timezone_name": bot_user.timezone or os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "timezone_top_options": _top_timezone_options(),
            "timezone_all_options": _standard_timezone_options(),
            "timezone_labels": _timezone_labels(_standard_timezone_options()),
            "work_days_json": json.dumps(bot_user.work_days, ensure_ascii=False)
            if bot_user.work_days is not None
            else "",
            "work_days_selected": bot_user.work_days if bot_user.work_days is not None else [0, 1, 2, 3, 4],
            "dnd": bool(bot_user.dnd),
            "error": None,
            "matrix_bot_mxid": _matrix_bot_mxid(),
            "csrf_token": csrf_token,
        },
    )
    if set_cookie:
        resp.set_cookie(CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return resp


@app.post("/me/settings")
async def me_settings_post(
    request: Request,
    notify_json: Annotated[str, Form()] = "",
    notify_preset: Annotated[str, Form()] = "all",
    notify_values: Annotated[list[str], Form()] = [],
    timezone_name: Annotated[str, Form()] = "",
    work_hours: Annotated[str, Form()] = "",
    work_hours_from: Annotated[str, Form()] = "",
    work_hours_to: Annotated[str, Form()] = "",
    work_days_json: Annotated[str, Form()] = "",
    work_days_values: Annotated[list[str], Form()] = [],
    dnd: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    notify_catalog, _versions_catalog = await _load_catalogs(session)
    notify_allowed = [item["key"] for item in notify_catalog]
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if getattr(user, "role", "") == "admin":
        return RedirectResponse(DASHBOARD_PATH, status_code=303)

    redmine_id = getattr(user, "redmine_id", None)
    if redmine_id is None:
        raise HTTPException(
            400,
            "Нет привязки к Redmine: настройте подписку через бота в Matrix или обратитесь к администратору.",
        )

    r = await session.execute(select(BotUser).where(BotUser.redmine_id == redmine_id))
    bot_user = r.scalar_one_or_none()
    if not bot_user:
        raise HTTPException(404, "BotUser не найден")

    if notify_preset == "all":
        bot_user.notify = ["all"]
    elif notify_preset == "new_only":
        bot_user.notify = ["new"]
    elif notify_preset == "overdue_only":
        bot_user.notify = ["overdue"]
    elif notify_preset == "custom":
        bot_user.notify = _normalize_notify(notify_values, notify_allowed)
    else:
        bot_user.notify = _parse_notify(notify_json)
    bot_user.timezone = (timezone_name or "").strip() or None
    if work_hours_from and work_hours_to:
        bot_user.work_hours = f"{work_hours_from.strip()}-{work_hours_to.strip()}"
    else:
        bot_user.work_hours = work_hours.strip() or None
    if work_days_values:
        bot_user.work_days = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        bot_user.work_days = _parse_work_days(work_days_json)
    bot_user.dnd = dnd in ("on", "true", "1")
    await session.flush()

    await _maybe_log_admin_crud(
        session,
        user,
        "self_settings",
        "update",
        {"bot_user_id": bot_user.id, "redmine_id": redmine_id},
    )
    return RedirectResponse("/me/settings", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════
# DB credentials management (zero-config deployment)
# ═══════════════════════════════════════════════════════════════════════════

# Путь к .env файлу в Docker-контейнере
_ENV_FILE_PATH = Path("/app/.env")


def _load_db_config_from_env() -> dict[str, str]:
    """Читает DB credentials из .env файла."""
    if not _ENV_FILE_PATH.exists():
        return {
            "postgres_user": "bot",
            "postgres_db": "via",
            "postgres_password": "",
            "app_master_key": "",
        }

    config = {}
    for line in _ENV_FILE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()

    return {
        "postgres_user": config.get("POSTGRES_USER", "bot"),
        "postgres_db": config.get("POSTGRES_DB", "via"),
        "postgres_password": config.get("POSTGRES_PASSWORD", ""),
        "app_master_key": config.get("APP_MASTER_KEY", ""),
    }


def _update_env_file(updates: dict[str, str]) -> None:
    """Обновляет переменные в .env файле, сохраняя остальные."""
    if not _ENV_FILE_PATH.exists():
        raise RuntimeError(".env file not found")

    lines = _ENV_FILE_PATH.read_text(encoding="utf-8").splitlines()
    new_lines = []
    updated_keys = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Добавляем новые ключи, которых не было в файле
    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    _ENV_FILE_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


@app.get("/settings/db-config", response_class=JSONResponse)
async def get_db_config(request: Request, session: AsyncSession = Depends(get_session)):
    """Возвращает текущие DB credentials из .env (только для admin)."""
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    config = _load_db_config_from_env()
    return {
        "ok": True,
        "postgres_user": config["postgres_user"],
        "postgres_db": config["postgres_db"],
        "postgres_password": config["postgres_password"],
        "app_master_key": config["app_master_key"],
    }


@app.post("/settings/db-config/regenerate", response_class=JSONResponse)
async def regenerate_db_config(
    request: Request,
    regenerate_password: Annotated[str, Form()] = "1",
    regenerate_key: Annotated[str, Form()] = "1",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    """
    Генерирует новые credentials и обновляет .env.

    После вызова необходимо перезапустить контейнеры bot и admin,
    чтобы они подхватили новые credentials.

    PostgreSQL пароль также обновляется через ALTER USER.
    """
    import secrets as _secrets

    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    current_config = _load_db_config_from_env()
    updates = {}

    # Генерируем новые credentials
    if regenerate_password in ("1", "true", "yes", "on"):
        updates["POSTGRES_PASSWORD"] = _secrets.token_urlsafe(32)

    if regenerate_key in ("1", "true", "yes", "on"):
        updates["APP_MASTER_KEY"] = _secrets.token_urlsafe(32)

    if not updates:
        raise HTTPException(400, "Нечего перегенерировать")

    # Обновляем .env файл
    _update_env_file(updates)

    # Обновляем пароль в PostgreSQL
    if "POSTGRES_PASSWORD" in updates:
        try:
            from sqlalchemy import text

            # Подключаемся к БД с текущими credentials и меняем пароль
            await session.execute(
                text("ALTER USER :username WITH PASSWORD :password"),
                {
                    "username": current_config["postgres_user"],
                    "password": updates["POSTGRES_PASSWORD"],
                },
            )
            await session.commit()
        except Exception as e:
            # Откатываем изменения в .env при ошибке
            _update_env_file(
                {k: current_config[k.replace("POSTGRES_", "").lower()] for k in updates if k in current_config}
            )
            raise HTTPException(500, f"Не удалось обновить пароль в PostgreSQL: {e}") from e

    logger.info(
        "db_credentials_regenerated actor=%s regenerated=%s",
        mask_identifier(user.login),
        list(updates.keys()),
    )

    return {
        "ok": True,
        "message": "Credentials обновлены. Перезапустите контейнеры: docker compose restart postgres bot admin",
        "regenerated": list(updates.keys()),
        "new_postgres_password": updates.get("POSTGRES_PASSWORD", ""),
        "new_app_master_key": updates.get("APP_MASTER_KEY", ""),
    }
