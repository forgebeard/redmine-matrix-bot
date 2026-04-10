"""Secrets routes: /secrets."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AppSecret
from database.session import get_session
from mail import mask_identifier
from security import encrypt_secret, load_master_key

router = APIRouter(tags=["secrets"])


def _admin() -> object:
    """Late import to avoid circular dependency with main.py."""
    import admin.main as _m
    return _m


@router.get("/secrets", response_class=HTMLResponse)
async def secrets_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    rows = await session.execute(select(AppSecret).order_by(AppSecret.name))
    items = list(rows.scalars().all())
    csrf_token, set_cookie = admin._ensure_csrf(request)
    resp = admin.templates.TemplateResponse(
        request,
        "secrets.html",
        {"items": items, "error": None, "csrf_token": csrf_token},
    )
    if set_cookie:
        resp.set_cookie(admin.CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=admin.COOKIE_SECURE, samesite="lax")
    return resp


@router.post("/secrets")
async def secrets_save(
    request: Request,
    name: Annotated[str, Form()],
    value: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
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
    admin._integration_status_cache.clear()
    admin.logger.info(
        "secret_updated name=%s actor=%s key_version=%s",
        name,
        mask_identifier(user.login),
        enc.key_version,
    )
    return RedirectResponse("/secrets", status_code=303)
