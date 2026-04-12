"""Auth routes: login, setup, logout, password reset."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.helpers import (
    AUTH_TOKEN_SALT,
    COOKIE_SECURE,
    CSRF_COOKIE_NAME,
    DASHBOARD_PATH,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    SETUP_PATH,
    _append_ops_to_events_log,
    _client_ip,
    _ensure_csrf,
    _generic_login_error,
    _has_admin,
    _login_allowed,
    _login_format_ok,
    _normalize_login,
    _now_utc,
    _rate_limiter,
    _verify_csrf,
    templates,
)
from database.models import BotAppUser, BotSession, PasswordResetToken
from database.session import get_session, get_session_factory
from mail import mask_identifier
from security import hash_password, token_hash, validate_password_policy, verify_password

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    csrf_token, set_cookie = _ensure_csrf(request)
    can_register_admin = False
    try:
        factory = get_session_factory()
        async with factory() as session:
            can_register_admin = not await _has_admin(session, use_cache=False)
    except Exception:
        can_register_admin = False
    resp = templates.TemplateResponse(
        request,
        "auth/login.html",
        {"error": None, "csrf_token": csrf_token, "can_register_admin": can_register_admin},
    )
    if set_cookie:
        resp.set_cookie(
            CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax"
        )
    return resp


@router.get(SETUP_PATH, response_class=HTMLResponse)
async def setup_page(request: Request, session: AsyncSession = Depends(get_session)):
    if await _has_admin(session):
        return RedirectResponse("/login", status_code=303)
    csrf_token, set_cookie = _ensure_csrf(request)
    resp = templates.TemplateResponse(
        request, "auth/setup.html", {"error": None, "csrf_token": csrf_token}
    )
    if set_cookie:
        resp.set_cookie(
            CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax"
        )
    return resp


@router.post(SETUP_PATH)
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
            request, "auth/setup.html", {"error": fmt_err, "csrf_token": csrf_token}, status_code=400
        )
    if not _login_allowed(login_n):
        return templates.TemplateResponse(
            request,
            "auth/setup.html",
            {
                "error": "Этот логин не разрешён (проверьте ADMIN_LOGINS в окружении).",
                "csrf_token": csrf_token,
            },
            status_code=403,
        )
    if (password or "") != (password_confirm or ""):
        return templates.TemplateResponse(
            request,
            "auth/setup.html",
            {"error": "Пароли не совпадают", "csrf_token": csrf_token},
            status_code=400,
        )
    ok, reason = validate_password_policy(password, login=login_n)
    if not ok:
        return templates.TemplateResponse(
            request, "auth/setup.html", {"error": reason, "csrf_token": csrf_token}, status_code=400
        )
    await session.execute(select(BotAppUser.id).where(BotAppUser.role == "admin").with_for_update())
    any_admin = await session.execute(
        select(BotAppUser.id).where(BotAppUser.role == "admin").limit(1)
    )
    if any_admin.scalar_one_or_none() is not None:
        return templates.TemplateResponse(
            request,
            "auth/setup.html",
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
    return RedirectResponse("/onboarding", status_code=303)


@router.post("/login")
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
            "auth/login.html",
            {"error": _generic_login_error(), "csrf_token": csrf_token},
            status_code=401,
        )
    fmt_ok, _ = _login_format_ok(login_n)
    if not fmt_ok or not _login_allowed(login_n):
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {"error": _generic_login_error(), "csrf_token": csrf_token},
            status_code=401,
        )
    r = await session.execute(select(BotAppUser).where(BotAppUser.login == login_n))
    user = r.scalar_one_or_none()
    if not user or not user.password_hash or not verify_password(user.password_hash, password):
        return templates.TemplateResponse(
            request,
            "auth/login.html",
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
    await session.commit()  # Явный commit до редиректа — чтобы middleware нашёл сессию
    # NOTE: get_session() dependency попытается сделать ещё один commit,
    # но это будет no-op для уже закоммиченной транзакции.
    resp = RedirectResponse(DASHBOARD_PATH, status_code=303)
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        str(st.session_token),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    _append_ops_to_events_log(f"Вход в панель login={mask_identifier(login_n)} ip={ip}")
    return resp


@router.get("/forgot-password")
async def forgot_password_page():
    return RedirectResponse("/login", status_code=303)


@router.post("/forgot-password")
async def forgot_password_post():
    return RedirectResponse("/login", status_code=303)


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str = ""):
    csrf_token, set_cookie = _ensure_csrf(request)
    resp = templates.TemplateResponse(
        request, "auth/reset_password.html", {"error": None, "token": token, "csrf_token": csrf_token}
    )
    if set_cookie:
        resp.set_cookie(
            CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax"
        )
    return resp


@router.post("/reset-password")
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
            "auth/reset_password.html",
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
            "auth/reset_password.html",
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
            "auth/reset_password.html",
            {"error": reason, "token": token, "csrf_token": csrf_token},
            status_code=400,
        )
    user.password_hash = hash_password(password)
    user.session_version = (user.session_version or 1) + 1
    rt.used_at = now
    await session.execute(delete(BotSession).where(BotSession.user_id == user.id))
    return RedirectResponse("/login", status_code=303)


@router.get("/logout")
async def logout(request: Request, session: AsyncSession = Depends(get_session)):
    cur = getattr(request.state, "current_user", None)
    actor = mask_identifier(cur.login) if cur is not None else "?"
    token_raw = request.cookies.get(SESSION_COOKIE_NAME, "")
    if token_raw:
        try:
            await session.execute(
                delete(BotSession).where(BotSession.session_token == uuid.UUID(token_raw))
            )
        except Exception:
            pass
    _append_ops_to_events_log(f"Выход из панели login={actor}")
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp
