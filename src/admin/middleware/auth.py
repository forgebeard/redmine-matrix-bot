"""Сессионная авторизация и CSRF cookie для защищённых страниц."""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi.responses import RedirectResponse
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from admin.constants import (
    COOKIE_SECURE,
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_IDLE_TIMEOUT_SECONDS,
    SETUP_PATH,
)
from admin.csrf import ensure_csrf as _ensure_csrf
from admin.session_logic import has_admin as db_has_admin, integration_status as load_integration_status
from admin.timeutil import now_utc
from database.models import BotAppUser, BotSession
from database.session import get_session_factory


class AuthMiddleware(BaseHTTPMiddleware):
    """Auth для админки через DB-сессии после login по email/password."""

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
            "/health/smtp",
            SETUP_PATH,
        ) or p.startswith("/docs") or p in (
            "/openapi.json",
            "/redoc",
        ):
            return await call_next(request)

        try:
            factory = get_session_factory()
            async with factory() as session:
                has_admin = await db_has_admin(session)
        except Exception:
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
                now = now_utc()
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

                sess.expires_at = now + timedelta(seconds=SESSION_IDLE_TIMEOUT_SECONDS)
                await session.flush()
                await session.commit()

                request.state.current_user = user
                request.state.integration_status = await load_integration_status(session)
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
