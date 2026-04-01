"""Управление контейнером бота через Docker API (start/stop/restart)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from admin.audit import audit_op
from admin.auth_helpers import client_ip
from admin.authz import require_admin
from admin.csrf import verify_csrf as _verify_csrf
from admin.runtime import logger, rate_limiter
from admin.templates_env import templates
from database.session import get_session, get_session_factory
from ops.docker_control import DockerControlError, control_service

router = APIRouter()


@router.get("/ops/postgres-connection", response_class=HTMLResponse)
async def postgres_connection_page(request: Request):
    """Показать параметры БД для подключения с хоста (пароль из смонтированного файла)."""
    require_admin(request)
    user = (os.getenv("POSTGRES_USER") or "bot").strip()
    db = (os.getenv("POSTGRES_DB") or "redmine_matrix").strip()
    pw_file = (os.getenv("DATABASE_PASSWORD_FILE") or "").strip()
    password_display = ""
    err = ""
    if pw_file:
        p = Path(pw_file)
        if p.is_file():
            password_display = p.read_text(encoding="utf-8").strip()
        else:
            err = f"Файл пароля не найден: {pw_file}"
    else:
        err = (
            "Пароль из файла недоступен (не задан DATABASE_PASSWORD_FILE). "
            "Используйте явный DATABASE_URL в окружении или стандартный compose с томом пароля."
        )
    return templates.TemplateResponse(
        request,
        "postgres_credentials.html",
        {
            "pg_user": user,
            "pg_db": db,
            "password": password_display,
            "error": err,
            "host_port_note": (
                "С хоста: 127.0.0.1 и порт из compose (часто 5433 → контейнер 5432). "
                "Внутри сети Docker: хост postgres, порт 5432."
            ),
        },
    )


def restart_in_background(actor_email: str | None) -> None:
    def _run() -> None:
        time.sleep(1.5)
        detail = ""
        status = "ok"
        try:
            control_service("restart")
            detail = "restart command accepted"
        except Exception as e:  # noqa: BLE001
            status = "error"
            detail = str(e)

        async def _persist() -> None:
            factory = get_session_factory()
            async with factory() as s:
                await audit_op(s, "BOT_RESTART", status, actor_email=actor_email, detail=detail)
                await s.commit()

        try:
            asyncio.run(_persist())
        except Exception:
            logger.exception("failed to persist restart audit")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


@router.post("/ops/bot/{action}")
async def bot_ops_action(
    request: Request,
    action: str,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    current = require_admin(request)
    ip = client_ip(request)
    if not rate_limiter.hit(f"ops:{ip}:{current.login}", limit=12, window_seconds=60):
        raise HTTPException(429, "Слишком много операций, попробуйте позже")

    allowed = {"start", "stop", "restart"}
    if action not in allowed:
        raise HTTPException(400, "Недопустимое действие")
    actor = current.login
    if action == "restart":
        await audit_op(session, "BOT_RESTART", "accepted", actor_email=actor, detail="scheduled")
        await session.commit()
        restart_in_background(actor)
        return RedirectResponse("/?ops=restart_accepted", status_code=303)

    try:
        res = control_service(action)
        await audit_op(
            session,
            f"BOT_{action.upper()}",
            "ok",
            actor_email=actor,
            detail=json.dumps(res, ensure_ascii=False),
        )
        await session.commit()
        return RedirectResponse(f"/?ops={action}_ok", status_code=303)
    except DockerControlError as e:
        await audit_op(
            session,
            f"BOT_{action.upper()}",
            "error",
            actor_email=actor,
            detail=str(e),
        )
        await session.commit()
        return RedirectResponse(f"/?ops={action}_error", status_code=303)
