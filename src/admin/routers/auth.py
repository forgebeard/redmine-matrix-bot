"""Публичные и начальные страницы: login, setup, onboarding, logout."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from admin.auth_helpers import (
    client_ip,
    generic_login_error,
    normalize_admin_login,
    validate_new_login_shape,
)
from admin.constants import (
    BOOTSTRAP_ADMIN_LOGIN,
    COOKIE_SECURE,
    CSRF_COOKIE_NAME,
    MUST_CHANGE_CREDENTIALS_PATH,
    ONBOARDING_SKIPPED_SECRET,
    REQUIRED_SECRET_NAMES,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    SETUP_PATH,
)
from admin.csrf import ensure_csrf as _ensure_csrf, verify_csrf as _verify_csrf
from admin.runtime import admin_exists_cache, integration_status_cache, logger, rate_limiter
from admin.session_logic import integration_status as load_integration_status
from admin.templates_env import templates
from admin.timeutil import now_utc
from database.models import AppSecret, BotAppUser, BotSession
from database.session import get_session
from mail import mask_login
from security import encrypt_secret, hash_password, load_master_key, validate_password_policy, verify_password

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    csrf_token, set_cookie = _ensure_csrf(request)
    resp = templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": None,
            "csrf_token": csrf_token,
            "bootstrap_hint": True,
        },
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
async def setup_page(request: Request):
    """Устарело: первый администратор создаётся миграцией (admin/admin), затем обязательная смена в панели."""
    return RedirectResponse("/login", status_code=303)


@router.post(SETUP_PATH)
async def setup_post(request: Request):
    return RedirectResponse("/login", status_code=303)


@router.get(MUST_CHANGE_CREDENTIALS_PATH, response_class=HTMLResponse)
async def bootstrap_credentials_get(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not getattr(user, "must_change_credentials", False):
        return RedirectResponse("/", status_code=303)
    csrf_token, set_cookie = _ensure_csrf(request)
    resp = templates.TemplateResponse(
        request,
        "bootstrap_credentials.html",
        {
            "error": None,
            "csrf_token": csrf_token,
            "bootstrap_login": BOOTSTRAP_ADMIN_LOGIN,
        },
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


@router.post(MUST_CHANGE_CREDENTIALS_PATH)
async def bootstrap_credentials_post(
    request: Request,
    login: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password_confirm: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    ip = client_ip(request)
    if not rate_limiter.hit(f"bootstrap_creds:ip:{ip}", limit=10, window_seconds=3600):
        csrf_ok, _ = _ensure_csrf(request)
        return templates.TemplateResponse(
            request,
            "bootstrap_credentials.html",
            {
                "error": "Слишком много попыток с этого адреса, попробуйте позже",
                "csrf_token": csrf_ok,
                "bootstrap_login": BOOTSTRAP_ADMIN_LOGIN,
            },
            status_code=429,
        )
    st_user = getattr(request.state, "current_user", None)
    if not st_user or not getattr(st_user, "must_change_credentials", False):
        return RedirectResponse("/login", status_code=303)
    r_user = await session.execute(select(BotAppUser).where(BotAppUser.id == st_user.id))
    user = r_user.scalar_one_or_none()
    if not user or not user.must_change_credentials:
        return RedirectResponse("/login", status_code=303)
    login_norm = normalize_admin_login(login)
    ok_shape, shape_reason = validate_new_login_shape(login_norm)
    if not ok_shape:
        return templates.TemplateResponse(
            request,
            "bootstrap_credentials.html",
            {"error": shape_reason, "csrf_token": csrf_token, "bootstrap_login": BOOTSTRAP_ADMIN_LOGIN},
            status_code=400,
        )
    if login_norm == BOOTSTRAP_ADMIN_LOGIN:
        return templates.TemplateResponse(
            request,
            "bootstrap_credentials.html",
            {
                "error": "Выберите другой логин (нельзя оставлять встроенный «admin»)",
                "csrf_token": csrf_token,
                "bootstrap_login": BOOTSTRAP_ADMIN_LOGIN,
            },
            status_code=400,
        )
    if password != password_confirm:
        return templates.TemplateResponse(
            request,
            "bootstrap_credentials.html",
            {"error": "Пароли не совпадают", "csrf_token": csrf_token, "bootstrap_login": BOOTSTRAP_ADMIN_LOGIN},
            status_code=400,
        )
    ok, reason = validate_password_policy(password, login=login_norm)
    if not ok:
        return templates.TemplateResponse(
            request,
            "bootstrap_credentials.html",
            {"error": reason, "csrf_token": csrf_token, "bootstrap_login": BOOTSTRAP_ADMIN_LOGIN},
            status_code=400,
        )
    taken = await session.execute(
        select(BotAppUser.id).where(BotAppUser.login == login_norm, BotAppUser.id != user.id).limit(1)
    )
    if taken.scalar_one_or_none() is not None:
        return templates.TemplateResponse(
            request,
            "bootstrap_credentials.html",
            {
                "error": "Такой логин уже занят",
                "csrf_token": csrf_token,
                "bootstrap_login": BOOTSTRAP_ADMIN_LOGIN,
            },
            status_code=400,
        )

    token_raw = request.cookies.get(SESSION_COOKIE_NAME, "")
    user.login = login_norm
    user.password_hash = hash_password(password)
    user.must_change_credentials = False
    user.session_version += 1
    await session.flush()
    if token_raw:
        try:
            tok = uuid.UUID(token_raw)
            await session.execute(
                update(BotSession)
                .where(BotSession.session_token == tok)
                .values(session_version=user.session_version)
            )
        except Exception:
            pass
    admin_exists_cache.invalidate()
    integration_status_cache.invalidate()

    istatus = await load_integration_status(session, use_cache=False)
    next_url = "/onboarding" if (not istatus["configured"] and not istatus["skipped"]) else "/"
    return RedirectResponse(next_url, status_code=303)


@router.post("/login")
async def login_post(
    request: Request,
    login: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    ip = client_ip(request)
    if not rate_limiter.hit(f"login:ip:{ip}", limit=5, window_seconds=60):
        raise HTTPException(429, "Слишком много попыток, попробуйте позже")

    login_norm = normalize_admin_login(login)
    if not login_norm or not password:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": generic_login_error(), "csrf_token": csrf_token},
            status_code=401,
        )
    r = await session.execute(select(BotAppUser).where(BotAppUser.login == login_norm))
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
    if getattr(user, "must_change_credentials", False):
        next_url = MUST_CHANGE_CREDENTIALS_PATH
    else:
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
    if getattr(user, "must_change_credentials", False):
        return RedirectResponse(MUST_CHANGE_CREDENTIALS_PATH, status_code=303)
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
    if getattr(user, "must_change_credentials", False):
        return RedirectResponse(MUST_CHANGE_CREDENTIALS_PATH, status_code=303)
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
            mask_login(user.login),
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
    if getattr(user, "must_change_credentials", False):
        return RedirectResponse(MUST_CHANGE_CREDENTIALS_PATH, status_code=303)
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
