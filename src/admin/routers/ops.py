"""Управление контейнером бота через Docker API (start/stop/restart)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from admin.audit import audit_op
from admin.auth_helpers import client_ip
from admin.csrf import verify_csrf as _verify_csrf
from admin.runtime import logger, rate_limiter
from database.session import get_session, get_session_factory
from ops.docker_control import DockerControlError, control_service

router = APIRouter()


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
    current = getattr(request.state, "current_user", None)
    if not current or getattr(current, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    ip = client_ip(request)
    if not rate_limiter.hit(f"ops:{ip}:{current.email}", limit=12, window_seconds=60):
        raise HTTPException(429, "Слишком много операций, попробуйте позже")

    allowed = {"start", "stop", "restart"}
    if action not in allowed:
        raise HTTPException(400, "Недопустимое действие")
    actor = current.email
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
