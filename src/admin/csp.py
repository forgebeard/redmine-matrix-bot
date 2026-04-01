"""CSP и базовые security-заголовки для HTML-ответов."""

from __future__ import annotations

import os

from starlette.requests import Request


def admin_csp_value() -> str | None:
    """
    Content-Security-Policy для HTML-ответов.
    ADMIN_CSP_POLICY — полная строка политики (приоритет).
    ADMIN_ENABLE_CSP=1 — встроенная политика под текущие CDN (htmx, FA, Google Fonts)
    и inline script/style (обработчики в шаблонах до выноса в .js).
    """
    explicit = (os.getenv("ADMIN_CSP_POLICY") or "").strip()
    if explicit:
        return explicit
    if os.getenv("ADMIN_ENABLE_CSP", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    return (
        "default-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "connect-src 'self';"
    )


async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    csp = admin_csp_value()
    if csp:
        response.headers["Content-Security-Policy"] = csp
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response
