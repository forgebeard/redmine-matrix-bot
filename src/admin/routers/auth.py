"""Публичные и начальные страницы: login, setup, onboarding, forgot/reset, logout."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.auth_helpers import client_ip, generic_login_error
from admin.constants import (
    AUTH_TOKEN_SALT,
    COOKIE_SECURE,
    CSRF_COOKIE_NAME,
    ONBOARDING_SKIPPED_SECRET,
    REQUIRED_SECRET_NAMES,
    RESET_TOKEN_TTL_SECONDS,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    SETUP_PATH,
    SHOW_DEV_TOKENS,
)
from admin.csrf import ensure_csrf as _ensure_csrf, verify_csrf as _verify_csrf
from admin.runtime import admin_exists_cache, integration_status_cache, logger, rate_limiter
from admin.session_logic import has_admin as db_has_admin, integration_status as load_integration_status
from admin.templates_env import templates
from admin.timeutil import now_utc
from database.models import AppSecret, BotAppUser, BotSession, PasswordResetToken
from database.session import get_session, get_session_factory
from mail import mask_email, send_reset_email
from security import (
    encrypt_secret,
    hash_password,
    load_master_key,
    make_reset_token,
    token_hash,
    validate_password_policy,
    verify_password,
)

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    csrf_token, set_cookie = _ensure_csrf(request)
    can_register_admin = False
    try:
        factory = get_session_factory()
        async with factory() as session:
            can_register_admin = not await db_has_admin(session, use_cache=False)
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


@router.get(SETUP_PATH, response_class=HTMLResponse)
async def setup_page(request: Request, session: AsyncSession = Depends(get_session)):
    if await db_has_admin(session):
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


@router.post(SETUP_PATH)
async def setup_post(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    ip = client_ip(request)
    if not rate_limiter.hit(f"setup:ip:{ip}", limit=10, window_seconds=3600):
        csrf_ok, _ = _ensure_csrf(request)
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "error": "Слишком много попыток с этого адреса, попробуйте позже",
                "csrf_token": csrf_ok,
            },
            status_code=429,
        )
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": "Введите корректный email", "csrf_token": csrf_token},
            status_code=400,
        )
    ok, reason = validate_password_policy(password, email=email)
    if not ok:
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"error": reason, "csrf_token": csrf_token},
            status_code=400,
        )
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
        email=email,
        role="admin",
        verified_at=now_utc(),
        password_hash=hash_password(password),
        session_version=1,
    )
    session.add(user)
    admin_exists_cache.invalidate()
    return RedirectResponse("/onboarding", status_code=303)


@router.post("/login")
async def login_post(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    ip = client_ip(request)
    if not rate_limiter.hit(f"login:ip:{ip}", limit=5, window_seconds=60):
        raise HTTPException(429, "Слишком много попыток, попробуйте позже")

    email = (email or "").strip().lower()
    if not email or not password:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": generic_login_error(), "csrf_token": csrf_token},
            status_code=401,
        )
    r = await session.execute(select(BotAppUser).where(BotAppUser.email == email))
    user = r.scalar_one_or_none()
    if not user or not user.password_hash or not verify_password(user.password_hash, password):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": generic_login_error(), "csrf_token": csrf_token},
            status_code=401,
        )
    now = now_utc()
    st = BotSession(
        session_token=uuid.uuid4(),
        user_id=user.id,
        expires_at=now + timedelta(seconds=SESSION_TTL_SECONDS),
        session_version=user.session_version,
    )
    session.add(st)
    await session.flush()
    istatus = await load_integration_status(session, use_cache=False)
    next_url = "/onboarding" if (not istatus["configured"] and not istatus["skipped"]) else "/"
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


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, session: AsyncSession = Depends(get_session)):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        return RedirectResponse("/login", status_code=303)
    status = await load_integration_status(session)
    csrf_token, set_cookie = _ensure_csrf(request)
    resp = templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "required_names": REQUIRED_SECRET_NAMES,
            "missing": status["missing"],
            "csrf_token": csrf_token,
            "error": None,
        },
    )
    if set_cookie:
        resp.set_cookie(CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return resp


@router.post("/onboarding/save")
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
            row = AppSecret(
                name=secret_name,
                ciphertext=enc.ciphertext,
                nonce=enc.nonce,
                key_version=enc.key_version,
            )
            session.add(row)
        else:
            row.ciphertext = enc.ciphertext
            row.nonce = enc.nonce
            row.key_version = enc.key_version
        logger.info(
            "secret_updated name=%s actor=%s key_version=%s",
            secret_name,
            mask_email(user.email),
            enc.key_version,
        )
    await session.execute(delete(AppSecret).where(AppSecret.name == ONBOARDING_SKIPPED_SECRET))
    integration_status_cache.invalidate()
    return RedirectResponse("/", status_code=303)


@router.post("/onboarding/skip")
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
        session.add(
            AppSecret(
                name=ONBOARDING_SKIPPED_SECRET,
                ciphertext=enc.ciphertext,
                nonce=enc.nonce,
                key_version=enc.key_version,
            )
        )
    integration_status_cache.invalidate()
    return RedirectResponse("/", status_code=303)


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    csrf_token, set_cookie = _ensure_csrf(request)
    resp = templates.TemplateResponse(
        request,
        "forgot_password.html",
        {"error": None, "ok": None, "csrf_token": csrf_token},
    )
    if set_cookie:
        resp.set_cookie(CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return resp


@router.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_post(
    request: Request,
    email: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    email = (email or "").strip().lower()
    ip = client_ip(request)
    if not rate_limiter.hit(f"forgot:ip:{ip}", limit=5, window_seconds=60):
        raise HTTPException(429, "Слишком много попыток, попробуйте позже")
    if not rate_limiter.hit(f"forgot:email:{email}", limit=3, window_seconds=3600):
        return templates.TemplateResponse(
            request,
            "forgot_password.html",
            {
                "error": "Слишком много запросов сброса, попробуйте позже",
                "ok": None,
                "csrf_token": csrf_token,
            },
            status_code=429,
        )
    r = await session.execute(select(BotAppUser).where(BotAppUser.email == email))
    user = r.scalar_one_or_none()
    if user:
        token = make_reset_token()
        row = PasswordResetToken(
            id=uuid.uuid4(),
            user_id=user.id,
            token_hash=token_hash(token, AUTH_TOKEN_SALT),
            requested_email=email,
            expires_at=now_utc() + timedelta(seconds=RESET_TOKEN_TTL_SECONDS),
            used_at=None,
        )
        session.add(row)
        await session.flush()
        reset_url = f"{request.base_url}reset-password?token={token}"
        sent, send_detail = send_reset_email(email, reset_url)
        logger.info(
            "password_reset_requested email=%s sent=%s detail=%s",
            mask_email(email),
            sent,
            send_detail,
        )
        dev_token = token if SHOW_DEV_TOKENS else None
        return templates.TemplateResponse(
            request,
            "forgot_password.html",
            {
                "error": None,
                "ok": "Если email существует, ссылка на сброс отправлена.",
                "dev_token": dev_token,
                "csrf_token": csrf_token,
            },
        )
    return templates.TemplateResponse(
        request,
        "forgot_password.html",
        {
            "error": None,
            "ok": "Если email существует, ссылка на сброс отправлена.",
            "csrf_token": csrf_token,
        },
    )


@router.get("/reset-password", response_class=HTMLResponse)
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


@router.post("/reset-password")
async def reset_password_post(
    request: Request,
    token: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    ip = client_ip(request)
    if not rate_limiter.hit(f"reset_pw:ip:{ip}", limit=20, window_seconds=900):
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {
                "error": "Слишком много попыток с этого адреса, попробуйте позже",
                "token": (token or "").strip(),
                "csrf_token": csrf_token,
            },
            status_code=429,
        )
    token = (token or "").strip()
    if not token or not password:
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            {"error": "Неверный или просроченный токен", "token": token, "csrf_token": csrf_token},
            status_code=401,
        )
    now = now_utc()
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
    ok, reason = validate_password_policy(password, email=user.email)
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


@router.get("/logout")
async def logout(request: Request, session: AsyncSession = Depends(get_session)):
    token_raw = request.cookies.get(SESSION_COOKIE_NAME, "")
    if token_raw:
        try:
            token_uuid = uuid.UUID(token_raw)
            await session.execute(delete(BotSession).where(BotSession.session_token == token_uuid))
        except Exception:
            pass

    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp
