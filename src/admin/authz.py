"""Проверка роли admin для защищённых страниц и форм."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request


def require_admin(request: Request) -> Any:
    """403, если нет сессии или роль не admin; иначе объект current_user."""
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    return user
