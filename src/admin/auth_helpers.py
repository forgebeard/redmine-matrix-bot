"""Мелкие хелперы для страниц входа и сброса пароля."""

from __future__ import annotations

from starlette.requests import Request


def generic_login_error() -> str:
    return "Неверный email или пароль"


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
