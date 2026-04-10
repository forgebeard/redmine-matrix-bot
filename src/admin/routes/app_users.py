"""App users routes: /app-users."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BotAppUser, BotSession, PasswordResetToken
from database.session import get_session
from mail import mask_identifier
from security import hash_password, validate_password_policy

router = APIRouter(tags=["app_users"])


def _admin() -> object:
    """Late import to avoid circular dependency with main.py."""
    import admin.main as _m
    return _m


@router.get("/app-users", response_class=HTMLResponse)
async def app_users_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    rows = await session.execute(select(BotAppUser).order_by(BotAppUser.login))
    users = list(rows.scalars().all())
    csrf_token, set_cookie = admin._ensure_csrf(request)
    resp = admin.templates.TemplateResponse(
        request,
        "app_users.html",
        {"users": users, "csrf_token": csrf_token},
    )
    if set_cookie:
        resp.set_cookie(admin.CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=admin.COOKIE_SECURE, samesite="lax")
    return resp


@router.post("/app-users/{user_id}/reset-password-admin")
async def app_user_reset_password_admin(
    request: Request,
    user_id: str,
    new_password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
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
        csrf_out, set_cookie = admin._ensure_csrf(request)
        resp = admin.templates.TemplateResponse(
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
            resp.set_cookie(admin.CSRF_COOKIE_NAME, csrf_out, httponly=True, secure=admin.COOKIE_SECURE, samesite="lax")
        return resp
    target.password_hash = hash_password(new_password)
    target.session_version = (target.session_version or 1) + 1
    await session.execute(delete(BotSession).where(BotSession.user_id == target.id))
    admin.logger.info(
        "admin_password_reset target=%s actor=%s",
        mask_identifier(target.login),
        mask_identifier(current.login),
    )
    return RedirectResponse("/app-users", status_code=303)


@router.post("/app-users/{user_id}/change-login-admin")
async def app_user_change_login_admin(
    request: Request,
    user_id: str,
    new_login: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
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
        csrf_out, set_cookie = admin._ensure_csrf(request)
        resp = admin.templates.TemplateResponse(
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
            resp.set_cookie(admin.CSRF_COOKIE_NAME, csrf_out, httponly=True, secure=admin.COOKIE_SECURE, samesite="lax")
        return resp

    new_login_n = admin._normalize_login(new_login)
    fmt_ok, fmt_err = admin._login_format_ok(new_login_n)
    if not fmt_ok:
        return await _err(fmt_err or "Некорректный логин")
    if not admin._login_allowed(new_login_n):
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
    await session.flush()
    admin.logger.info(
        "admin_login_changed old=%s new=%s actor=%s",
        mask_identifier(old_login),
        mask_identifier(new_login_n),
        mask_identifier(current.login),
    )
    return RedirectResponse("/app-users", status_code=303)
