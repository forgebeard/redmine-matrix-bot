"""
Веб-админка: пользователи бота и маршруты Matrix (Postgres).

Запуск: uvicorn admin_main:app --host 0.0.0.0 --port 8080
Требуется DATABASE_URL (доступ к UI — через email/password).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from html import escape as html_escape
import os
import sys
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated
from datetime import datetime, timedelta

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.staticfiles import StaticFiles
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nio import AsyncClient

from database.load_config import row_counts
from database.models import (
    AppSecret,
    BotOpsAudit,
    BotAppUser,
    BotSession,
    BotUser,
    MatrixRoomBinding,
    StatusRoomRoute,
    SupportGroup,
    VersionRoomRoute,
)
from database.session import get_session, get_session_factory
from mail import mask_email
from security import encrypt_secret, hash_password, load_master_key, token_hash, validate_password_policy

from matrix_send import room_send_with_retry
from ops.docker_control import DockerControlError, control_service, get_service_status

from admin.constants import (
    AUTH_TOKEN_SALT,
    COOKIE_SECURE,
    CSRF_COOKIE_NAME,
    GROUP_UNASSIGNED_NAME,
    NOTIFY_TYPE_KEYS,
    REDMINE_API_KEY,
    REDMINE_URL,
    RUNTIME_STATUS_FILE,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
)
from admin.csp import admin_csp_value as _admin_csp_value, security_headers_middleware
from admin.csrf import ensure_csrf as _ensure_csrf, verify_csrf as _verify_csrf
from admin.lifespan import admin_lifespan as _admin_lifespan
from admin.routers.auth import router as auth_router
from admin.routers.health import router as health_router
from admin.templates_env import admin_asset_version as _admin_asset_version, templates
from admin.runtime import (
    admin_exists_cache,
    integration_status_cache,
    logger,
    process_started_at,
    rate_limiter,
    redmine_search_breaker,
)
from admin.auth_helpers import client_ip as _client_ip, generic_login_error as _generic_login_error
from admin.middleware.auth import AuthMiddleware
from admin.session_logic import runtime_status_from_file
from admin.timeutil import now_utc as _now_utc

app = FastAPI(
    title="Matrix bot control panel",
    version="0.1.0",
    lifespan=_admin_lifespan,
)
app.include_router(health_router)
app.include_router(auth_router)
app.middleware("http")(security_headers_middleware)

_STATIC_ROOT = _ROOT / "static"
if _STATIC_ROOT.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_ROOT)), name="static")


def _token_hash(value: str) -> str:
    return hashlib.sha256((value + AUTH_TOKEN_SALT).encode("utf-8")).hexdigest()


async def _audit_op(
    session: AsyncSession,
    action: str,
    status: str,
    actor_email: str | None = None,
    detail: str | None = None,
) -> None:
    row = BotOpsAudit(
        actor_email=(actor_email or "").strip().lower() or None,
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
                "actor": actor_email or "",
                "detail": detail or "",
                "ts": _now_utc().isoformat(),
            },
            ensure_ascii=False,
        )
    )


app.add_middleware(AuthMiddleware)


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    nu, ns, nv = await row_counts(session)
    runtime_file = runtime_status_from_file()
    try:
        runtime_docker = get_service_status()
    except DockerControlError as e:
        runtime_docker = {"state": "error", "detail": str(e), "service": os.getenv("DOCKER_TARGET_SERVICE", "bot")}
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "users_count": nu,
            "status_routes_count": ns,
            "version_routes_count": nv,
            "runtime_status": {
                "uptime_s": int(time.monotonic() - process_started_at),
                "live": True,
                "ready": True,
                "cycle": runtime_file,
                "docker": runtime_docker,
            },
        },
    )


def _restart_in_background(actor_email: str | None) -> None:
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
                await _audit_op(s, "BOT_RESTART", status, actor_email=actor_email, detail=detail)
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
    if not rate_limiter.hit(f"ops:{ip}:{current.email}", limit=12, window_seconds=60):
        raise HTTPException(429, "Слишком много операций, попробуйте позже")

    allowed = {"start", "stop", "restart"}
    if action not in allowed:
        raise HTTPException(400, "Недопустимое действие")
    actor = current.email
    if action == "restart":
        await _audit_op(session, "BOT_RESTART", "accepted", actor_email=actor, detail="scheduled")
        await session.commit()
        _restart_in_background(actor)
        return RedirectResponse("/?ops=restart_accepted", status_code=303)

    try:
        res = control_service(action)
        await _audit_op(
            session,
            f"BOT_{action.upper()}",
            "ok",
            actor_email=actor,
            detail=json.dumps(res, ensure_ascii=False),
        )
        await session.commit()
        return RedirectResponse(f"/?ops={action}_ok", status_code=303)
    except DockerControlError as e:
        await _audit_op(
            session,
            f"BOT_{action.upper()}",
            "error",
            actor_email=actor,
            detail=str(e),
        )
        await session.commit()
        return RedirectResponse(f"/?ops={action}_error", status_code=303)


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
    integration_status_cache.invalidate()
    logger.info(
        "secret_updated name=%s actor=%s key_version=%s",
        name,
        mask_email(user.email),
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
    rows = await session.execute(select(BotAppUser).order_by(BotAppUser.email))
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
    ok, reason = validate_password_policy(new_password, email=target.email)
    if not ok:
        raise HTTPException(400, reason)
    target.password_hash = hash_password(new_password)
    target.session_version = (target.session_version or 1) + 1
    await session.execute(delete(BotSession).where(BotSession.user_id == target.id))
    logger.info("admin_password_reset target=%s actor=%s", mask_email(target.email), mask_email(current.email))
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
    rows = list((await session.execute(stmt)).scalars().all())
    return templates.TemplateResponse(
        request,
        "groups_list.html",
        {
            "items": rows,
            "q": q,
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
        {"title": "Новая группа", "g": None, "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow")},
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
    return templates.TemplateResponse(
        request,
        "group_form.html",
        {"title": "Редактирование группы", "g": row, "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow")},
    )


@app.post("/groups")
async def groups_create(
    request: Request,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str, Form()] = "",
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
    row = SupportGroup(
        name=n,
        room_id=(room_id or "").strip(),
        timezone=(timezone_name or "").strip() or None,
        is_active=is_active in ("1", "on", "true"),
    )
    session.add(row)
    await session.flush()
    return RedirectResponse("/groups", status_code=303)


@app.post("/groups/{group_id}")
async def groups_update(
    request: Request,
    group_id: int,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str, Form()] = "",
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
    n = (name or "").strip()
    if not n:
        raise HTTPException(400, "Название обязательно")
    row.name = n
    row.room_id = (room_id or "").strip()
    row.timezone = (timezone_name or "").strip() or None
    row.is_active = is_active in ("1", "on", "true")
    return RedirectResponse("/groups", status_code=303)


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
        if row.group_id is None:
            key = GROUP_UNASSIGNED_NAME
        else:
            key = groups_by_id.get(row.group_id).name if groups_by_id.get(row.group_id) else GROUP_UNASSIGNED_NAME
        grouped.setdefault(key, []).append(row)

    return templates.TemplateResponse(
        request,
        "users_list.html",
        {
            "users": rows,
            "grouped_users": grouped,
            "groups": groups_rows,
            "groups_by_id": groups_by_id,
            "q": q,
            "group_filter": group_id,
            "group_unassigned_name": GROUP_UNASSIGNED_NAME,
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
            "groups": groups_rows,
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
    return RedirectResponse("/users", status_code=303)


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
            "groups": groups_rows,
            "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
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
    row.redmine_id = redmine_id
    row.display_name = display_name.strip() or None
    row.group_id = int(group_id) if str(group_id).isdigit() else None
    row.room = room.strip()
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
    return RedirectResponse("/users", status_code=303)


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
    if redmine_search_breaker.blocked():
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
        redmine_search_breaker.on_failure()
        return HTMLResponse(f'<option value="">Ошибка поиска: {html_escape(err)}</option>')
    redmine_search_breaker.on_success()
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


# --- Маршруты по статусу ---


@app.get("/routes/status", response_class=HTMLResponse)
async def routes_status(
    request: Request,
    q: str = "",
    added: int = 0,
    skipped: int = 0,
    error: str = "",
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    stmt = select(StatusRoomRoute)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                StatusRoomRoute.status_key.ilike(like),
                StatusRoomRoute.room_id.ilike(like),
            )
        )
    stmt = stmt.order_by(StatusRoomRoute.status_key)
    r = await session.execute(stmt)
    rows = list(r.scalars().all())
    room_map: dict[str, list[str]] = {}
    for row in rows:
        room_map.setdefault(row.room_id, []).append(row.status_key)
    room_map = {k: sorted(v) for k, v in sorted(room_map.items(), key=lambda x: x[0])}
    return templates.TemplateResponse(
        request,
        "routes_status.html",
        {"rows": rows, "room_map": room_map, "added": added, "skipped": skipped, "error": error, "q": q},
    )


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
        return RedirectResponse("/routes/status?error=Заполните+оба+поля", status_code=303)
    exists = await session.execute(select(StatusRoomRoute).where(StatusRoomRoute.status_key == key))
    if exists.scalar_one_or_none():
        return RedirectResponse("/routes/status?added=0&skipped=1", status_code=303)
    session.add(StatusRoomRoute(status_key=key, room_id=room))
    return RedirectResponse("/routes/status?added=1&skipped=0", status_code=303)


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
    return RedirectResponse(f"/routes/status?added={added}&skipped={skipped}", status_code=303)


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
    return RedirectResponse("/routes/status", status_code=303)


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


# --- Matrix room binding (one-time code) ---


@app.get("/matrix/bind", response_class=HTMLResponse)
async def matrix_bind_page(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)

    redmine_id = getattr(user, "redmine_id", None) or ""
    return templates.TemplateResponse(
        request,
        "matrix_bind.html",
        {"redmine_id": redmine_id, "room_id": "", "code_sent": False, "dev_code": None, "error": None},
    )


@app.post("/matrix/bind/start")
async def matrix_bind_start(
    request: Request,
    redmine_id: Annotated[int, Form()],
    room_id: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)

    room_id = room_id.strip()
    if not room_id:
        raise HTTPException(400, "room_id пуст")

    # Пользователь может связать комнату только для своей redmine_id.
    # Если redmine_id ещё не задан — позволяем впервые.
    if getattr(user, "redmine_id", None) is not None and getattr(user, "redmine_id", None) != redmine_id:
        raise HTTPException(403, "Можно привязать комнату только для своей Redmine-учётки")

    # 6-значный цифровой код.
    code = "".join(secrets.choice("0123456789") for _ in range(6))
    code_hash = _token_hash(code)
    expires_at = _now_utc() + timedelta(seconds=MATRIX_CODE_TTL_SECONDS)

    row = MatrixRoomBinding(
        id=uuid.uuid4(),
        user_id=user.id,
        redmine_id=redmine_id,
        room_id=room_id,
        verify_code_hash=code_hash,
        expires_at=expires_at,
        used_at=None,
    )
    session.add(row)
    await session.flush()

    # Отправляем код в Matrix (если есть конфигурация).
    try:
        HOMESERVER = (os.getenv("MATRIX_HOMESERVER") or "").strip()
        ACCESS_TOKEN = (os.getenv("MATRIX_ACCESS_TOKEN") or "").strip()
        MATRIX_USER_ID = (os.getenv("MATRIX_USER_ID") or "").strip()
        MATRIX_DEVICE_ID = (os.getenv("MATRIX_DEVICE_ID") or "").strip()
        if HOMESERVER and ACCESS_TOKEN and MATRIX_USER_ID:
            mclient = AsyncClient(HOMESERVER)
            mclient.access_token = ACCESS_TOKEN
            mclient.user_id = MATRIX_USER_ID
            mclient.device_id = MATRIX_DEVICE_ID
            await room_send_with_retry(
                mclient,
                room_id,
                {
                    "msgtype": "m.text",
                    "body": f"Код подтверждения: {code}",
                    "format": "org.matrix.custom.html",
                    "formatted_body": f"<b>Код подтверждения:</b> {code}",
                },
            )
            await mclient.close()
    except Exception:
        # В dev/CI может не быть Matrix-конфига — UI всё равно работает как верификация по коду.
        pass

    dev_echo = os.getenv("MATRIX_CODE_DEV_ECHO", "0").strip().lower() in ("1", "true", "yes", "on")
    dev_line = f"<p><b>Dev code:</b> {code}</p>" if dev_echo else ""

    return templates.TemplateResponse(
        request,
        "matrix_bind.html",
        {
            "redmine_id": redmine_id,
            "room_id": room_id,
            "code_sent": True,
            "dev_code": code if dev_echo else None,
            "error": None,
        },
    )


@app.post("/matrix/bind/confirm")
async def matrix_bind_confirm(
    request: Request,
    redmine_id: Annotated[int, Form()],
    room_id: Annotated[str, Form()],
    code: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)

    room_id = room_id.strip()
    code = (code or "").strip()
    if not room_id or not code:
        raise HTTPException(400, "room_id и code обязательны")

    if getattr(user, "redmine_id", None) is not None and getattr(user, "redmine_id", None) != redmine_id:
        raise HTTPException(403, "Can’t change redmine_id after it is set")

    code_hash = _token_hash(code)
    now = _now_utc()

    r = await session.execute(
        select(MatrixRoomBinding).where(
            MatrixRoomBinding.user_id == user.id,
            MatrixRoomBinding.redmine_id == redmine_id,
            MatrixRoomBinding.room_id == room_id,
            MatrixRoomBinding.used_at.is_(None),
            MatrixRoomBinding.expires_at > now,
            MatrixRoomBinding.verify_code_hash == code_hash,
        )
    )
    binding = r.scalars().first()
    if not binding:
        return templates.TemplateResponse(
            request,
            "matrix_bind.html",
            {
                "redmine_id": redmine_id,
                "room_id": room_id,
                "code_sent": True,
                "dev_code": None,
                "error": "Неверный код или срок истёк.",
            },
            status_code=401,
        )

    binding.used_at = now

    # Обновляем привязку в app-user (redmine_id можно поставить только 1 раз).
    app_user = await session.get(BotAppUser, user.id)
    if app_user and app_user.redmine_id is None:
        app_user.redmine_id = redmine_id

    # Upsert bot_user (комната для отправки).
    r2 = await session.execute(select(BotUser).where(BotUser.redmine_id == redmine_id))
    bot_user = r2.scalar_one_or_none()
    if bot_user:
        bot_user.room = room_id
    else:
        session.add(BotUser(redmine_id=redmine_id, room=room_id))

    return RedirectResponse("/", status_code=303)


# --- User self-service: настройки ---


@app.get("/me/settings", response_class=HTMLResponse)
async def me_settings_get(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)

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
                "error": "Сначала привяжите комнату через Matrix binding.",
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

    redmine_id = getattr(user, "redmine_id", None)
    if redmine_id is None:
        raise HTTPException(400, "Сначала привяжите комнату через Matrix binding.")

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
