"""Мелкие хелперы для страниц входа и первичной настройки."""

from __future__ import annotations

from starlette.requests import Request


def normalize_admin_login(raw: str) -> str:
    return (raw or "").strip().lower()


def validate_new_login_shape(login_norm: str) -> tuple[bool, str | None]:
    """Проверка логина при /setup (не миграция: допускаем любые уже сохранённые строки)."""
    if len(login_norm) < 3:
        return False, "Логин не короче 3 символов"
    if len(login_norm) > 255:
        return False, "Логин слишком длинный"
    for ch in login_norm:
        if ch.isspace():
            return False, "Логин не должен содержать пробелы"
    return True, None


def generic_login_error() -> str:
    return "Неверный логин или пароль"


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
