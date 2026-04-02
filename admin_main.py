"""
Веб-админка: пользователи бота и маршруты Matrix (Postgres).

Запуск: uvicorn admin_main:app --host 0.0.0.0 --port 8080
Требуется DATABASE_URL (доступ к UI — через логин и пароль).
"""

from __future__ import annotations

import asyncio
import json
import re
from html import escape as html_escape
import logging
import os
import sys
import secrets
import threading
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Annotated
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from jinja2 import Environment, FileSystemLoader

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    AppSecret,
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
    encrypt_secret,
    hash_password,
    load_master_key,
    make_reset_token,
    token_hash,
    validate_password_policy,
    verify_password,
)

from ops.docker_control import DockerControlError, control_service, get_service_status

_templates_dir = str(_ROOT / "templates" / "admin")
# В некоторых наборах версий Jinja2/Starlette кэш шаблонов может приводить к TypeError
# (unhashable type: 'dict'). Отключаем кэш, чтобы /login работал стабильно.
_jinja_env = Environment(
    loader=FileSystemLoader(_templates_dir),
    autoescape=True,
    cache_size=0,
)


def _admin_asset_version() -> str:
    """Query string для cache-bust ссылок на `/static/...` (см. `ADMIN_ASSET_VERSION`)."""
    v = (os.getenv("ADMIN_ASSET_VERSION") or "").strip()
    return v if v else "1"


_jinja_env.globals["asset_version"] = _admin_asset_version
_jinja_env.globals["bot_timezone"] = lambda: (os.getenv("BOT_TIMEZONE") or "Europe/Moscow")
templates = Jinja2Templates(env=_jinja_env)

app = FastAPI(title="Matrix bot control panel", version="0.1.0")

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

AUTH_TOKEN_SALT = os.getenv("AUTH_TOKEN_SALT", "dev-token-salt")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
RESET_TOKEN_TTL_SECONDS = int(os.getenv("RESET_TOKEN_TTL_SECONDS", "1800"))
RESET_COOLDOWN_SECONDS = int(os.getenv("RESET_COOLDOWN_SECONDS", "90"))

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
ONBOARDING_SKIPPED_SECRET = "__onboarding_skipped"


def _matrix_bot_mxid() -> str:
    """MXID бота из .env — подсказка в «Мои настройки» (без отдельной страницы привязки)."""
    return (os.getenv("MATRIX_USER_ID") or "").strip()
NOTIFY_TYPES = [
    ("new", "Новая задача"),
    ("info", "Информация предоставлена"),
    ("reminder", "Напоминание"),
    ("overdue", "Просроченная задача"),
    ("status_change", "Изменение статуса"),
    ("issue_updated", "Обновление задачи"),
    ("reopened", "Переоткрыта"),
]
NOTIFY_TYPE_KEYS = [k for k, _ in NOTIFY_TYPES]

ADMIN_BOOTSTRAP_FIRST_ADMIN = (os.getenv("ADMIN_BOOTSTRAP_FIRST_ADMIN", "0").strip().lower() in ("1", "true", "yes", "on"))

_LOGIN_RE = re.compile(r"^[a-zA-Z0-9@._+-]{3,255}$")


def _admin_allowlist() -> frozenset[str]:
    raw = (os.getenv("ADMIN_LOGINS") or "").strip()
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


def _normalize_login(raw: str) -> str:
    return (raw or "").strip().lower()


def _login_format_ok(login: str) -> tuple[bool, str | None]:
    if not login:
        return False, "Введите логин"
    if not _LOGIN_RE.fullmatch(login):
        return False, "Логин: 3–255 символов, латиница, цифры и символы . _ + - @"
    return True, None


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


def _append_ops_to_events_log(message: str) -> None:
    """
    Дублирует операции Docker из панели в файл «Событий» (по умолчанию data/bot.log),
    чтобы страница /events показывала то же, что видит админ в UI (лог бота при этом не заменяется).
    """
    path: Path | None = None
    try:
        path = _admin_events_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        now = _now_utc()
        ts = now.strftime("%Y-%m-%d %H:%M:%S") + f",{now.microsecond // 1000:03d}"
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


def _normalized_group_filter_key(name: str) -> str:
    """Нормализация имени для сравнения с подписью фильтра (без дублей «Все группы» в select)."""
    return unicodedata.normalize("NFKC", name).strip().casefold()


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
    "stop_ok": "Docker принял остановку контейнера бота (или контейнер уже был остановлен). Счётчик «uptime панели» на дашборде — это работа веб-админки, а не бота.",
    "stop_error": "Не удалось остановить бот. Проверьте DOCKER_HOST, docker-socket-proxy и имя сервиса (DOCKER_TARGET_SERVICE, метки compose).",
    "start_ok": "Docker принял запуск контейнера бота (или он уже был запущен). Счётчик uptime на карточке — у веб-панели admin, не процесса бота.",
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


class _AdminExistsCache:
    def __init__(self):
        self.value: bool | None = None
        self.expires_ts: float = 0.0

    def get(self) -> bool | None:
        if self.value is None:
            return None
        if datetime.now().timestamp() >= self.expires_ts:
            return None
        return self.value

    def set(self, value: bool):
        self.value = value
        self.expires_ts = datetime.now().timestamp() + ADMIN_EXISTS_CACHE_TTL_SECONDS

    def invalidate(self):
        self.value = None
        self.expires_ts = 0.0


_admin_exists_cache = _AdminExistsCache()


class _IntegrationStatusCache:
    def __init__(self):
        self.value: dict | None = None
        self.expires_ts: float = 0.0

    def get(self) -> dict | None:
        if self.value is None:
            return None
        if datetime.now().timestamp() >= self.expires_ts:
            return None
        return self.value

    def set(self, value: dict):
        self.value = value
        self.expires_ts = datetime.now().timestamp() + INTEGRATION_STATUS_CACHE_TTL_SECONDS

    def invalidate(self):
        self.value = None
        self.expires_ts = 0.0


_integration_status_cache = _IntegrationStatusCache()


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


_process_started_at = time.monotonic()


def _runtime_status_from_file() -> dict:
    p = Path(RUNTIME_STATUS_FILE)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


async def _has_admin(session: AsyncSession, use_cache: bool = True) -> bool:
    if use_cache:
        cached = _admin_exists_cache.get()
        if cached is not None:
            return cached
    any_admin = await session.execute(
        select(BotAppUser.id).where(BotAppUser.role == "admin").limit(1)
    )
    value = any_admin.scalar_one_or_none() is not None
    _admin_exists_cache.set(value)
    return value


async def _integration_status(session: AsyncSession, use_cache: bool = True) -> dict:
    if use_cache:
        cached = _integration_status_cache.get()
        if cached is not None:
            return cached
    rows = await session.execute(select(AppSecret.name).where(AppSecret.name.in_(REQUIRED_SECRET_NAMES + [ONBOARDING_SKIPPED_SECRET])))
    names = {r[0] for r in rows.all()}
    missing = [name for name in REQUIRED_SECRET_NAMES if name not in names]
    status = {
        "configured": len(missing) == 0,
        "missing": missing,
        "skipped": ONBOARDING_SKIPPED_SECRET in names,
    }
    _integration_status_cache.set(status)
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


REDMINE_URL = (os.getenv("REDMINE_URL") or "").strip()
REDMINE_API_KEY = (os.getenv("REDMINE_API_KEY") or "").strip()


app.add_middleware(AuthMiddleware)


@app.on_event("startup")
async def startup_checks():
    # Fail-fast: без master key нельзя безопасно работать с encrypted secrets.
    try:
        load_master_key()
    except SecurityError as e:
        raise RuntimeError(f"startup failed: {e}") from e


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/live")
async def health_live():
    return {"status": "live"}


@app.get("/health/ready")
async def health_ready(session: AsyncSession = Depends(get_session)):
    try:
        await session.execute(select(BotAppUser.id).limit(1))
        load_master_key()
        get_service_status()
    except SecurityError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except DockerControlError as e:
        raise HTTPException(status_code=503, detail=f"runtime backend: {e}")
    except Exception:
        raise HTTPException(status_code=503, detail="service not ready")
    return {"status": "ready"}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    csrf_token, set_cookie = _ensure_csrf(request)
    can_register_admin = False
    try:
        factory = get_session_factory()
        async with factory() as session:
            # Do not use cache here: page should immediately reflect setup completion.
            can_register_admin = not await _has_admin(session, use_cache=False)
    except Exception:
        can_register_admin = False
    resp = templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "csrf_token": csrf_token, "can_register_admin": can_register_admin},
    )
    if set_cookie:
        resp.set_cookie(
            CSRF_COOKIE_NAME,
            csrf_token,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="lax",
        )
    return resp


@app.get(SETUP_PATH, response_class=HTMLResponse)
async def setup_page(request: Request, session: AsyncSession = Depends(get_session)):
    if await _has_admin(session):
        return RedirectResponse("/login", status_code=303)
    csrf_token, set_cookie = _ensure_csrf(request)
    resp = templates.TemplateResponse(
        request,
        "setup.html",
        {"error": None, "csrf_token": csrf_token},
    )
    if set_cookie:
        resp.set_cookie(
            CSRF_COOKIE_NAME,
            csrf_token,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="lax",
        )
    return resp


@app.post(SETUP_PATH)
async def setup_post(
    request: Request,
    login: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    login_n = _normalize_login(login)
    fmt_ok, fmt_err = _login_format_ok(login_n)
    if not fmt_ok:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": fmt_err, "csrf_token": csrf_token},
            status_code=400,
        )
    if not _login_allowed(login_n):
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "error": "Этот логин не разрешён (проверьте ADMIN_LOGINS в окружении).",
                "csrf_token": csrf_token,
            },
            status_code=403,
        )
    if (password or "") != (password_confirm or ""):
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": "Пароли не совпадают", "csrf_token": csrf_token},
            status_code=400,
        )
    ok, reason = validate_password_policy(password, login=login_n)
    if not ok:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": reason, "csrf_token": csrf_token},
            status_code=400,
        )
    # Protect from race: lock admin rows.
    await session.execute(
        select(BotAppUser.id).where(BotAppUser.role == "admin").with_for_update()
    )
    any_admin = await session.execute(
        select(BotAppUser.id).where(BotAppUser.role == "admin").limit(1)
    )
    if any_admin.scalar_one_or_none() is not None:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": "Администратор уже создан", "csrf_token": csrf_token},
            status_code=409,
        )
    user = BotAppUser(
        id=uuid.uuid4(),
        login=login_n,
        role="admin",
        verified_at=_now_utc(),
        password_hash=hash_password(password),
        session_version=1,
    )
    session.add(user)
    _admin_exists_cache.invalidate()
    return RedirectResponse("/onboarding", status_code=303)


@app.post("/login")
async def login_post(
    request: Request,
    login: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    ip = _client_ip(request)
    if not _rate_limiter.hit(f"login:ip:{ip}", limit=5, window_seconds=60):
        raise HTTPException(429, "Слишком много попыток, попробуйте позже")

    login_n = _normalize_login(login)
    if not login_n or not password:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": _generic_login_error(), "csrf_token": csrf_token},
            status_code=401,
        )
    fmt_ok, _ = _login_format_ok(login_n)
    if not fmt_ok or not _login_allowed(login_n):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": _generic_login_error(), "csrf_token": csrf_token},
            status_code=401,
        )
    r = await session.execute(select(BotAppUser).where(BotAppUser.login == login_n))
    user = r.scalar_one_or_none()
    if not user or not user.password_hash or not verify_password(user.password_hash, password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": _generic_login_error(), "csrf_token": csrf_token},
            status_code=401,
        )
    now = _now_utc()
    st = BotSession(
        session_token=uuid.uuid4(),
        user_id=user.id,
        expires_at=now + timedelta(seconds=SESSION_TTL_SECONDS),
        session_version=user.session_version,
    )
    session.add(st)
    await session.flush()
    integration_status = await _integration_status(session, use_cache=False)
    next_url = "/onboarding" if (not integration_status["configured"] and not integration_status["skipped"]) else "/"
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        str(st.session_token),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    return resp


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, session: AsyncSession = Depends(get_session)):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        return RedirectResponse("/login", status_code=303)
    csrf_token, set_cookie = _ensure_csrf(request)
    resp = templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "csrf_token": csrf_token,
            "error": None,
        },
    )
    if set_cookie:
        resp.set_cookie(CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return resp


@app.post("/onboarding/save")
async def onboarding_save(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        return RedirectResponse("/login", status_code=303)
    key = load_master_key()
    form = await request.form()
    for secret_name in REQUIRED_SECRET_NAMES:
        raw = form.get(f"secret_{secret_name}", "")
        value = (raw or "").strip()
        if not value:
            continue
        enc = encrypt_secret(value, key=key)
        r = await session.execute(select(AppSecret).where(AppSecret.name == secret_name))
        row = r.scalar_one_or_none()
        if row is None:
            row = AppSecret(name=secret_name, ciphertext=enc.ciphertext, nonce=enc.nonce, key_version=enc.key_version)
            session.add(row)
        else:
            row.ciphertext = enc.ciphertext
            row.nonce = enc.nonce
            row.key_version = enc.key_version
        logger.info("secret_updated name=%s actor=%s key_version=%s", secret_name, mask_identifier(user.login), enc.key_version)
    # onboarding is complete once values were submitted; remove skip marker.
    await session.execute(delete(AppSecret).where(AppSecret.name == ONBOARDING_SKIPPED_SECRET))
    _integration_status_cache.invalidate()
    return RedirectResponse("/", status_code=303)


@app.post("/onboarding/skip")
async def onboarding_skip(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        return RedirectResponse("/login", status_code=303)
    key = load_master_key()
    r = await session.execute(select(AppSecret).where(AppSecret.name == ONBOARDING_SKIPPED_SECRET))
    row = r.scalar_one_or_none()
    if row is None:
        enc = encrypt_secret("1", key=key)
        session.add(AppSecret(name=ONBOARDING_SKIPPED_SECRET, ciphertext=enc.ciphertext, nonce=enc.nonce, key_version=enc.key_version))
    _integration_status_cache.invalidate()
    return RedirectResponse("/", status_code=303)


@app.get("/forgot-password")
async def forgot_password_page():
    """Самообслуживания сброса нет: только администратор или скрипт с доступом к БД."""
    return RedirectResponse("/login", status_code=303)


@app.post("/forgot-password")
async def forgot_password_post():
    return RedirectResponse("/login", status_code=303)


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str = ""):
    csrf_token, set_cookie = _ensure_csrf(request)
    resp = templates.TemplateResponse(
        request,
        "reset_password.html",
        {"error": None, "token": token, "csrf_token": csrf_token},
    )
    if set_cookie:
        resp.set_cookie(CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return resp


@app.post("/reset-password")
async def reset_password_post(
    request: Request,
    token: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    token = (token or "").strip()
    if not token or not password:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"error": "Неверный или просроченный токен", "token": token, "csrf_token": csrf_token},
            status_code=401,
        )
    now = _now_utc()
    r = await session.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash(token, AUTH_TOKEN_SALT),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
    )
    rt = r.scalar_one_or_none()
    if not rt:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"error": "Неверный или просроченный токен", "token": token, "csrf_token": csrf_token},
            status_code=401,
        )
    u = await session.execute(select(BotAppUser).where(BotAppUser.id == rt.user_id))
    user = u.scalar_one_or_none()
    if not user:
        return RedirectResponse("/login", status_code=303)
    ok, reason = validate_password_policy(password, login=user.login)
    if not ok:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"error": reason, "token": token, "csrf_token": csrf_token},
            status_code=400,
        )
    user.password_hash = hash_password(password)
    user.session_version = (user.session_version or 1) + 1
    rt.used_at = now
    await session.execute(delete(BotSession).where(BotSession.user_id == user.id))
    return RedirectResponse("/login", status_code=303)


@app.get("/logout")
async def logout(request: Request, session: AsyncSession = Depends(get_session)):
    token_raw = request.cookies.get(SESSION_COOKIE_NAME, "")
    if token_raw:
        try:
            token_uuid = uuid.UUID(token_raw)
            await session.execute(
                delete(BotSession).where(BotSession.session_token == token_uuid)
            )
        except Exception:
            pass

    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    runtime_file = _runtime_status_from_file()
    try:
        runtime_docker = get_service_status()
    except DockerControlError as e:
        runtime_docker = {"state": "error", "detail": str(e), "service": os.getenv("DOCKER_TARGET_SERVICE", "bot")}
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
            "runtime_status": {
                "uptime_s": int(time.monotonic() - _process_started_at),
                "live": True,
                "ready": True,
                "cycle": runtime_file,
                "docker": runtime_docker,
            },
            "dash": dash,
            "integration_status": integration_status,
            "ops_flash": ops_flash,
        },
    )


@app.get("/dash/service-strip", response_class=HTMLResponse)
async def dash_service_strip(request: Request):
    """Фрагмент дашборда: uptime процесса admin, Docker-состояние бота, хвост runtime_status (для HTMX poll)."""
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    runtime_file = _runtime_status_from_file()
    try:
        runtime_docker = get_service_status()
    except DockerControlError as e:
        runtime_docker = {"state": "error", "detail": str(e), "service": os.getenv("DOCKER_TARGET_SERVICE", "bot")}
    uptime_s = int(time.monotonic() - _process_started_at)
    svc = html_escape(str(runtime_docker.get("service", "bot")))
    st = html_escape(str(runtime_docker.get("state", "unknown")))
    cname = runtime_docker.get("container_name") or ""
    cname_html = f' <span class="muted">({html_escape(cname)})</span>' if cname else ""
    cycle = runtime_file or {}
    last_at = html_escape(str(cycle.get("last_cycle_at") or "—"))
    dur = html_escape(str(cycle.get("last_cycle_duration_s") or "—"))
    err_n = html_escape(str(cycle.get("error_count") or 0))
    html = (
        '<p class="muted">Uptime <strong>веб-панели</strong> (процесс admin): '
        f"<strong>{uptime_s}с</strong> — растёт, пока работает контейнер админки; кнопки Start/Stop "
        f"управляют <strong>контейнером бота</strong> в Docker, не этой панелью.</p>"
        f"<p class=\"muted\">Бот (compose-сервис <strong>{svc}</strong>): состояние <strong>{st}</strong>{cname_html}.</p>"
        f'<p class="muted">Последний цикл: {last_at}; длительность: {dur}с; ошибок: {err_n}.</p>'
    )
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


@app.post("/ops/bot/{action}")
async def bot_ops_action(
    request: Request,
    action: str,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    current = getattr(request.state, "current_user", None)
    if not current or getattr(current, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    ip = _client_ip(request)
    if not _rate_limiter.hit(f"ops:{ip}:{current.login}", limit=12, window_seconds=60):
        raise HTTPException(429, "Слишком много операций, попробуйте позже")

    allowed = {"start", "stop", "restart"}
    if action not in allowed:
        raise HTTPException(400, "Недопустимое действие")
    actor = current.login
    if action == "restart":
        await _audit_op(session, "BOT_RESTART", "accepted", actor_login=actor, detail="scheduled")
        await session.commit()
        _append_ops_to_events_log(f"Docker bot/restart scheduled by={actor}")
        _restart_in_background(actor)
        return RedirectResponse("/?ops=restart_accepted", status_code=303)

    ops_q = f"{action}_error"
    ops_detail_err: str | None = None
    res_ok: dict | None = None
    try:
        res_ok = control_service(action)
        await _audit_op(
            session,
            f"BOT_{action.upper()}",
            "ok",
            actor_login=actor,
            detail=json.dumps(res_ok, ensure_ascii=False),
        )
        ops_q = f"{action}_ok"
    except DockerControlError as e:
        logger.warning("bot_ops DockerControlError action=%s: %s", action, e)
        ops_detail_err = str(e)
        await _audit_op(
            session,
            f"BOT_{action.upper()}",
            "error",
            actor_login=actor,
            detail=str(e)[:2000],
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("bot_ops unexpected error action=%s", action)
        ops_detail_err = str(e)
        await _audit_op(
            session,
            f"BOT_{action.upper()}",
            "error",
            actor_login=actor,
            detail=str(e)[:2000],
        )
    try:
        await session.commit()
    except Exception:
        logger.exception("bot_ops commit failed action=%s", action)
        await session.rollback()
        return RedirectResponse("/?ops=ops_commit_error", status_code=303)
    if action in ("start", "stop"):
        if ops_q == f"{action}_ok":
            r = res_ok or {}
            cid = str(r.get("container_id") or "")
            http_st = r.get("docker_http_status")
            http_part = f" http_status={http_st}" if http_st is not None else ""
            _append_ops_to_events_log(
                f"Docker bot/{action} ok by={actor} container_id={cid[:20]}{http_part}"
            )
        elif ops_q == f"{action}_error":
            _append_ops_to_events_log(
                f"Docker bot/{action} failed by={actor}: {_truncate_ops_detail(ops_detail_err or 'unknown', 400)}"
            )
    q: dict[str, str] = {"ops": ops_q}
    if ops_detail_err and ops_q.endswith("_error"):
        q["ops_detail"] = _truncate_ops_detail(ops_detail_err)
    return RedirectResponse("/?" + urlencode(q), status_code=303)


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
        raise HTTPException(400, reason)
    target.password_hash = hash_password(new_password)
    target.session_version = (target.session_version or 1) + 1
    await session.execute(delete(BotSession).where(BotSession.user_id == target.id))
    logger.info(
        "admin_password_reset target=%s actor=%s",
        mask_identifier(target.login),
        mask_identifier(current.login),
    )
    return RedirectResponse("/app-users", status_code=303)


# --- Пользователи ---


@app.get("/groups", response_class=HTMLResponse)
async def groups_list(
    request: Request,
    q: str = "",
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
            "list_total": len(rows),
        },
    )


@app.get("/groups/new", response_class=HTMLResponse)
async def groups_new(
    request: Request,
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    return templates.TemplateResponse(
        request,
        "group_form.html",
        {
            "title": "Новая группа",
            "g": None,
            "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "status_routes": [],
            "status_err": "",
            "status_msg": "",
            "notify_json": '["all"]',
            "notify_preset": "all",
            "notify_selected": ["all"],
            "initial_version_keys": "",
        },
    )


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
    return templates.TemplateResponse(
        request,
        "group_form.html",
        {
            "title": "Редактирование группы",
            "g": row,
            "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "status_routes": status_rows,
            "status_err": status_err,
            "status_msg": status_msg,
            "version_routes": version_rows,
            "version_err": version_err,
            "version_msg": version_msg,
            "notify_json": json.dumps(row.notify, ensure_ascii=False),
            "notify_preset": _notify_preset(row.notify),
            "notify_selected": row.notify or ["all"],
        },
    )


@app.post("/groups")
async def groups_create(
    request: Request,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str, Form()] = "",
    initial_status_keys: Annotated[str, Form()] = "",
    initial_version_keys: Annotated[str, Form()] = "",
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
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    n = (name or "").strip()
    if not n:
        raise HTTPException(400, "Название обязательно")
    if n == GROUP_UNASSIGNED_NAME:
        raise HTTPException(400, "Это имя зарезервировано для системы")
    room = (room_id or "").strip()
    if not room:
        raise HTTPException(400, "Укажите ID комнаты группы")
    status_keys = _parse_status_keys_list(initial_status_keys)
    if not status_keys:
        raise HTTPException(400, "Добавьте хотя бы один статус")
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
        notify = _normalize_notify(notify_values)
    else:
        notify = _parse_notify(notify_json)
    row = SupportGroup(
        name=n,
        room_id=room,
        timezone=(timezone_name or "").strip() or None,
        is_active=is_active in ("1", "on", "true"),
        notify=notify,
        work_hours=wh,
        work_days=wd,
        dnd=dnd in ("1", "on", "true"),
    )
    session.add(row)
    await session.flush()
    rid = row.id
    for key in status_keys:
        ex = await session.execute(select(StatusRoomRoute.id).where(StatusRoomRoute.status_key == key))
        if ex.scalar_one_or_none():
            continue
        session.add(StatusRoomRoute(status_key=key, room_id=room))
    for vkey in _parse_status_keys_list(initial_version_keys):
        ex = await session.execute(
            select(GroupVersionRoute.id).where(
                GroupVersionRoute.group_id == rid,
                GroupVersionRoute.version_key == vkey,
            )
        )
        if ex.scalar_one_or_none():
            continue
        session.add(GroupVersionRoute(group_id=rid, version_key=vkey, room_id=room))
    return RedirectResponse(f"/groups/{rid}/edit", status_code=303)


@app.post("/groups/{group_id}")
async def groups_update(
    request: Request,
    group_id: int,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str, Form()] = "",
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
        notify = _normalize_notify(notify_values)
    else:
        notify = _parse_notify(notify_json)
    old_room = (row.room_id or "").strip()
    new_room = (room_id or "").strip()
    row.name = n
    row.room_id = new_room
    row.timezone = (timezone_name or "").strip() or None
    row.is_active = is_active in ("1", "on", "true")
    row.notify = notify
    row.work_hours = wh
    row.work_days = wd
    row.dnd = dnd in ("1", "on", "true")
    if old_room and new_room and old_room != new_room:
        await session.execute(
            update(StatusRoomRoute).where(StatusRoomRoute.room_id == old_room).values(room_id=new_room)
        )
        await session.execute(
            update(GroupVersionRoute)
            .where(GroupVersionRoute.group_id == group_id, GroupVersionRoute.room_id == old_room)
            .values(room_id=new_room)
        )
    return RedirectResponse(f"/groups/{group_id}/edit", status_code=303)


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
    await session.delete(rte)
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
    await session.delete(rte)
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
        await session.delete(row)
    return RedirectResponse("/groups", status_code=303)


@app.get("/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    q: str = "",
    group_id: int | None = None,
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
    return templates.TemplateResponse(
        request,
        "user_form.html",
        {
            "title": "Новый пользователь",
            "u": None,
            "notify_json": '["all"]',
            "notify_preset": "all",
            "notify_selected": ["all"],
            "groups": _groups_assignable(groups_rows),
            "group_unassigned_display": GROUP_UNASSIGNED_DISPLAY,
            "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
        },
    )


def _parse_notify(raw: str) -> list:
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else ["all"]
    except json.JSONDecodeError:
        return ["all"]


def _normalize_notify(values: list[str] | None) -> list[str]:
    vals = [v.strip() for v in (values or []) if v and v.strip()]
    if not vals:
        return ["all"]
    if "all" in vals:
        return ["all"]
    allowed = [v for v in vals if v in NOTIFY_TYPE_KEYS]
    return allowed or ["all"]


def _notify_preset(notify: list | None) -> str:
    data = _normalize_notify([str(x) for x in (notify or [])])
    if "all" in data:
        return "all"
    if set(data) == {"new"}:
        return "new_only"
    if set(data) == {"overdue"}:
        return "overdue_only"
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
    work_hours: Annotated[str, Form()] = "",
    work_hours_from: Annotated[str, Form()] = "",
    work_hours_to: Annotated[str, Form()] = "",
    work_days_json: Annotated[str, Form()] = "",
    work_days_values: Annotated[list[str], Form()] = [],
    dnd: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
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
        notify = _normalize_notify(notify_values)
    else:
        notify = _parse_notify(notify_json)
    row = BotUser(
        redmine_id=redmine_id,
        display_name=display_name.strip() or None,
        group_id=int(group_id) if str(group_id).isdigit() else None,
        department=None,
        room=room.strip(),
        notify=notify,
        work_hours=wh,
        work_days=wd,
        dnd=dnd in ("on", "true", "1"),
    )
    session.add(row)
    await session.flush()
    return RedirectResponse(f"/users/{row.id}/edit", status_code=303)


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
    return templates.TemplateResponse(
        request,
        "user_form.html",
        {
            "title": f"Пользователь Redmine {row.redmine_id}",
            "u": row,
            "notify_json": json.dumps(row.notify, ensure_ascii=False),
            "notify_preset": _notify_preset(row.notify),
            "notify_selected": row.notify or ["all"],
            "groups": _groups_assignable(groups_rows),
            "group_unassigned_display": GROUP_UNASSIGNED_DISPLAY,
            "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "version_routes": version_rows,
            "version_err": version_err,
            "version_msg": version_msg,
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
    work_hours: Annotated[str, Form()] = "",
    work_hours_from: Annotated[str, Form()] = "",
    work_hours_to: Annotated[str, Form()] = "",
    work_days_json: Annotated[str, Form()] = "",
    work_days_values: Annotated[list[str], Form()] = [],
    dnd: Annotated[str, Form()] = "",
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
    old_room = (row.room or "").strip()
    new_room = room.strip()
    row.redmine_id = redmine_id
    row.display_name = display_name.strip() or None
    row.group_id = int(group_id) if str(group_id).isdigit() else None
    row.room = new_room
    if notify_preset == "all":
        row.notify = ["all"]
    elif notify_preset == "new_only":
        row.notify = ["new"]
    elif notify_preset == "overdue_only":
        row.notify = ["overdue"]
    elif notify_preset == "custom":
        row.notify = _normalize_notify(notify_values)
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
    if old_room and new_room and old_room != new_room:
        await session.execute(
            update(UserVersionRoute)
            .where(UserVersionRoute.bot_user_id == user_id, UserVersionRoute.room_id == old_room)
            .values(room_id=new_room)
        )
    return RedirectResponse(f"/users/{user_id}/edit", status_code=303)


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
    await session.delete(rte)
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
        await session.delete(row)
    return RedirectResponse("/users", status_code=303)


# --- Redmine: поиск users по имени/логину ---


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

    if not REDMINE_URL or not REDMINE_API_KEY:
        return HTMLResponse('<option value="">Redmine не настроен (нет URL/API key)</option>')

    def _do_search() -> tuple[list[dict], str | None]:
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError, URLError

        params = urlencode({"name": q, "limit": str(limit_i)})
        url = f"{REDMINE_URL.rstrip('/')}/users.json?{params}"
        req = Request(url, headers={"X-Redmine-API-Key": REDMINE_API_KEY})
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


def _fetch_redmine_user_by_id(redmine_user_id: int) -> tuple[dict | None, str | None]:
    """GET /users/:id.json → (user dict, None) или (None, error_code)."""
    if not REDMINE_URL or not REDMINE_API_KEY:
        return None, "not_configured"
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    url = f"{REDMINE_URL.rstrip('/')}/users/{redmine_user_id}.json"
    req = Request(url, headers={"X-Redmine-API-Key": REDMINE_API_KEY})
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
async def redmine_user_lookup(request: Request, user_id: int):
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

    raw, err = await asyncio.to_thread(_fetch_redmine_user_by_id, user_id)
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
    session.add(VersionRoomRoute(version_key=version_key.strip(), room_id=room_id.strip()))
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
    await session.execute(delete(VersionRoomRoute).where(VersionRoomRoute.id == row_id))
    return RedirectResponse("/routes/version", status_code=303)


# --- События (хвост файла лога бота) ---


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    return templates.TemplateResponse(request, "events.html", {})


@app.get("/events/tail", response_class=HTMLResponse)
async def events_log_tail(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    text = _read_log_tail(_admin_events_log_path())
    return HTMLResponse(f'<pre class="log-tail" id="events-log-pre">{html_escape(text)}</pre>')


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
        return RedirectResponse("/", status_code=303)

    redmine_id = getattr(user, "redmine_id", None)
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
                "work_hours": "",
                "work_hours_from": "",
                "work_hours_to": "",
                "work_days_json": "",
                "work_days_selected": [0, 1, 2, 3, 4],
                "dnd": False,
                "error": (
                    "Учётная запись в панели ещё не связана с Redmine. "
                    "Подписка на уведомления настраивается через бота в Matrix "
                    "(см. docs/MATRIX_ONBOARDING_PLAN.md) или попросите администратора завести вас в разделе «Пользователи»."
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

    resp = templates.TemplateResponse(
        request,
        "my_settings.html",
        {
            "room": bot_user.room,
            "notify_json": json.dumps(bot_user.notify, ensure_ascii=False)
            if bot_user.notify is not None
            else '["all"]',
            "notify_preset": _notify_preset(bot_user.notify),
            "notify_selected": bot_user.notify or ["all"],
            "work_hours": bot_user.work_hours or "",
            "work_hours_from": _parse_work_hours_range(bot_user.work_hours or "")[0],
            "work_hours_to": _parse_work_hours_range(bot_user.work_hours or "")[1],
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
    work_hours: Annotated[str, Form()] = "",
    work_hours_from: Annotated[str, Form()] = "",
    work_hours_to: Annotated[str, Form()] = "",
    work_days_json: Annotated[str, Form()] = "",
    work_days_values: Annotated[list[str], Form()] = [],
    dnd: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if getattr(user, "role", "") == "admin":
        return RedirectResponse("/", status_code=303)

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
        bot_user.notify = _normalize_notify(notify_values)
    else:
        bot_user.notify = _parse_notify(notify_json)
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

    return RedirectResponse("/me/settings", status_code=303)
