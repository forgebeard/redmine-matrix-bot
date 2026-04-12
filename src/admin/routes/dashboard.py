"""Dashboard routes: /, /dashboard, /dash/service-strip."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException

from database.session import get_session
from ops.docker_control import DockerControlError

router = APIRouter(tags=["dashboard"])


def _admin() -> object:
    """Late import to avoid circular dependency with main.py.

    NOTE: Must use 'src.admin.main' to match test imports
    (tests import 'src.admin.main', not 'admin.main').
    """
    import admin.main as _m

    return _m


# ── GET /, /dashboard ────────────────────────────────────────────────────────


async def _dashboard_page(request: Request, session: AsyncSession):
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    runtime_file = admin._runtime_status_from_file()
    try:
        runtime_docker = admin.get_service_status()
    except DockerControlError as e:
        runtime_docker = {
            "state": "error",
            "detail": str(e),
            "service": os.getenv("DOCKER_TARGET_SERVICE", "bot"),
            "container_name": "",
            "docker_status": "",
            "started_at": "",
            "running": False,
        }
    tz = (os.getenv("BOT_TIMEZONE") or "Europe/Moscow").strip()
    service_ctx = admin.service_card_context(runtime_docker, runtime_file, tz)
    dash = await admin._dashboard_counts(session)
    integration_status = await admin._integration_status(session)
    ops_flash = admin._ops_flash_message(
        request.query_params.get("ops"),
        request.query_params.get("ops_detail"),
    )
    return admin.templates.TemplateResponse(
        request,
        "panel/dashboard.html",
        {
            "runtime_status": {"cycle": runtime_file},
            "service_ctx": service_ctx,
            "dash": dash,
            "integration_status": integration_status,
            "ops_flash": ops_flash,
        },
    )


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    return await _dashboard_page(request, session)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    return await _dashboard_page(request, session)


# ── GET /dash/service-strip ──────────────────────────────────────────────────


@router.get("/dash/service-strip", response_class=HTMLResponse)
async def dash_service_strip(request: Request):
    """Фрагмент карточки «Сервис» (HTMX poll): Docker + runtime_status.json."""
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    runtime_file = admin._runtime_status_from_file()
    try:
        runtime_docker = admin.get_service_status()
    except DockerControlError as e:
        runtime_docker = {
            "state": "error",
            "detail": str(e),
            "service": os.getenv("DOCKER_TARGET_SERVICE", "bot"),
            "container_name": "",
            "docker_status": "",
            "started_at": "",
            "running": False,
        }
    tz = (os.getenv("BOT_TIMEZONE") or "Europe/Moscow").strip()
    ctx = admin.service_card_context(runtime_docker, runtime_file, tz)
    html = admin._jinja_env.get_template("partials/service_metrics.html").render(service_ctx=ctx)
    return HTMLResponse(html)
