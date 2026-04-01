"""Double-submit CSRF для форм и HTMX."""

from __future__ import annotations

import secrets

from fastapi import HTTPException
from starlette.requests import Request

from admin.constants import CSRF_COOKIE_NAME


def ensure_csrf(request: Request) -> tuple[str, bool]:
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if token:
        return token, False
    return secrets.token_urlsafe(24), True


def verify_csrf(request: Request, form_token: str = "") -> None:
    """Проверка double-submit CSRF: поле формы или заголовок X-CSRF-Token (для HTMX)."""
    token = (form_token or "").strip()
    if not token:
        token = request.headers.get("X-CSRF-Token", "").strip()
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not cookie_token or not token or token != cookie_token:
        raise HTTPException(status_code=400, detail="Некорректный CSRF токен")
