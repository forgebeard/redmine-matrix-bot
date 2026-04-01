"""Главная страница админки (дашборд)."""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from admin.authz import require_admin
from admin.runtime import process_started_at
from admin.session_logic import runtime_status_from_file
from admin.templates_env import templates
from database.load_config import row_counts
from database.session import get_session
from ops.docker_control import DockerControlError, get_service_status

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    require_admin(request)
    nu, ns, nv = await row_counts(session)
    runtime_file = runtime_status_from_file()
    try:
        runtime_docker = get_service_status()
    except DockerControlError as e:
        runtime_docker = {"state": "error", "detail": str(e), "service": os.getenv("DOCKER_TARGET_SERVICE", "bot")}
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "users_count": nu,
            "status_routes_count": ns,
            "version_routes_count": nv,
            "runtime_status": {
                "uptime_s": int(time.monotonic() - process_started_at),
                "live": True,
                "ready": True,
                "cycle": runtime_file,
                "docker": runtime_docker,
            },
        },
    )
