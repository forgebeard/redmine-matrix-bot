"""Привязка Matrix-комнаты к учётке через одноразовый код."""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from nio import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.constants import MATRIX_CODE_TTL_SECONDS
from admin.csrf import verify_csrf as _verify_csrf
from admin.matrix_tokens import hash_binding_code
from admin.templates_env import templates
from admin.timeutil import now_utc as _now_utc
from database.app_secret_values import load_decrypted_secrets, merge_secret
from database.models import BotAppUser, BotUser, MatrixRoomBinding
from database.session import get_session
from matrix_send import room_send_with_retry

router = APIRouter()


@router.get("/matrix/bind", response_class=HTMLResponse)
async def matrix_bind_page(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)

    redmine_id = getattr(user, "redmine_id", None) or ""
    return templates.TemplateResponse(
        request,
        "matrix_bind.html",
        {"redmine_id": redmine_id, "room_id": "", "code_sent": False, "dev_code": None, "error": None},
    )


@router.post("/matrix/bind/start")
async def matrix_bind_start(
    request: Request,
    redmine_id: Annotated[int, Form()],
    room_id: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)

    room_id = room_id.strip()
    if not room_id:
        raise HTTPException(400, "room_id пуст")

    if getattr(user, "redmine_id", None) is not None and getattr(user, "redmine_id", None) != redmine_id:
        raise HTTPException(403, "Можно привязать комнату только для своей Redmine-учётки")

    code = "".join(secrets.choice("0123456789") for _ in range(6))
    code_hash = hash_binding_code(code)
    expires_at = _now_utc() + timedelta(seconds=MATRIX_CODE_TTL_SECONDS)

    row = MatrixRoomBinding(
        id=uuid.uuid4(),
        user_id=user.id,
        redmine_id=redmine_id,
        room_id=room_id,
        verify_code_hash=code_hash,
        expires_at=expires_at,
        used_at=None,
    )
    session.add(row)
    await session.flush()

    try:
        db_mx = await load_decrypted_secrets(
            session,
            ("MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN", "MATRIX_USER_ID", "MATRIX_DEVICE_ID"),
        )
        homeserver = merge_secret(db_mx, "MATRIX_HOMESERVER", os.getenv("MATRIX_HOMESERVER"))
        access_token = merge_secret(db_mx, "MATRIX_ACCESS_TOKEN", os.getenv("MATRIX_ACCESS_TOKEN"))
        matrix_user_id = merge_secret(db_mx, "MATRIX_USER_ID", os.getenv("MATRIX_USER_ID"))
        matrix_device_id = merge_secret(db_mx, "MATRIX_DEVICE_ID", os.getenv("MATRIX_DEVICE_ID"))
        if homeserver and access_token and matrix_user_id:
            mclient = AsyncClient(homeserver)
            mclient.access_token = access_token
            mclient.user_id = matrix_user_id
            mclient.device_id = matrix_device_id
            await room_send_with_retry(
                mclient,
                room_id,
                {
                    "msgtype": "m.text",
                    "body": f"Код подтверждения: {code}",
                    "format": "org.matrix.custom.html",
                    "formatted_body": f"<b>Код подтверждения:</b> {code}",
                },
            )
            await mclient.close()
    except Exception:
        pass

    dev_echo = os.getenv("MATRIX_CODE_DEV_ECHO", "0").strip().lower() in ("1", "true", "yes", "on")

    return templates.TemplateResponse(
        request,
        "matrix_bind.html",
        {
            "redmine_id": redmine_id,
            "room_id": room_id,
            "code_sent": True,
            "dev_code": code if dev_echo else None,
            "error": None,
        },
    )


@router.post("/matrix/bind/confirm")
async def matrix_bind_confirm(
    request: Request,
    redmine_id: Annotated[int, Form()],
    room_id: Annotated[str, Form()],
    code: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)

    room_id = room_id.strip()
    code = (code or "").strip()
    if not room_id or not code:
        raise HTTPException(400, "room_id и code обязательны")

    if getattr(user, "redmine_id", None) is not None and getattr(user, "redmine_id", None) != redmine_id:
        raise HTTPException(403, "Can’t change redmine_id after it is set")

    code_hash = hash_binding_code(code)
    now = _now_utc()

    r = await session.execute(
        select(MatrixRoomBinding).where(
            MatrixRoomBinding.user_id == user.id,
            MatrixRoomBinding.redmine_id == redmine_id,
            MatrixRoomBinding.room_id == room_id,
            MatrixRoomBinding.used_at.is_(None),
            MatrixRoomBinding.expires_at > now,
            MatrixRoomBinding.verify_code_hash == code_hash,
        )
    )
    binding = r.scalars().first()
    if not binding:
        return templates.TemplateResponse(
            request,
            "matrix_bind.html",
            {
                "redmine_id": redmine_id,
                "room_id": room_id,
                "code_sent": True,
                "dev_code": None,
                "error": "Неверный код или срок истёк.",
            },
            status_code=401,
        )

    binding.used_at = now

    app_user = await session.get(BotAppUser, user.id)
    if app_user and app_user.redmine_id is None:
        app_user.redmine_id = redmine_id

    r2 = await session.execute(select(BotUser).where(BotUser.redmine_id == redmine_id))
    bot_user = r2.scalar_one_or_none()
    if bot_user:
        bot_user.room = room_id
    else:
        session.add(BotUser(redmine_id=redmine_id, room=room_id))

    return RedirectResponse("/", status_code=303)
