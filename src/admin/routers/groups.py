"""Группы поддержки: список, создание, редактирование, удаление."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.authz import require_admin
from admin.csrf import verify_csrf as _verify_csrf
from admin.templates_env import templates
from database.models import SupportGroup
from database.session import get_session

router = APIRouter()


@router.get("/groups", response_class=HTMLResponse)
async def groups_list(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
):
    require_admin(request)
    q = (q or "").strip()
    stmt = select(SupportGroup)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(SupportGroup.name.ilike(like), SupportGroup.room_id.ilike(like)))
    stmt = stmt.order_by(SupportGroup.is_active.desc(), SupportGroup.name.asc())
    rows = list((await session.execute(stmt)).scalars().all())
    return templates.TemplateResponse(
        request,
        "groups_list.html",
        {
            "items": rows,
            "q": q,
        },
    )


@router.get("/groups/new", response_class=HTMLResponse)
async def groups_new(request: Request):
    require_admin(request)
    return templates.TemplateResponse(
        request,
        "group_form.html",
        {"title": "Новая группа", "g": None, "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow")},
    )


@router.get("/groups/{group_id}/edit", response_class=HTMLResponse)
async def groups_edit(
    request: Request,
    group_id: int,
    session: AsyncSession = Depends(get_session),
):
    require_admin(request)
    row = await session.get(SupportGroup, group_id)
    if not row:
        raise HTTPException(404, "Группа не найдена")
    return templates.TemplateResponse(
        request,
        "group_form.html",
        {"title": "Редактирование группы", "g": row, "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow")},
    )


@router.post("/groups")
async def groups_create(
    request: Request,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    require_admin(request)
    n = (name or "").strip()
    if not n:
        raise HTTPException(400, "Название обязательно")
    row = SupportGroup(
        name=n,
        room_id=(room_id or "").strip(),
        timezone=(timezone_name or "").strip() or None,
        is_active=is_active in ("1", "on", "true"),
    )
    session.add(row)
    await session.flush()
    return RedirectResponse("/groups", status_code=303)


@router.post("/groups/{group_id}")
async def groups_update(
    request: Request,
    group_id: int,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    require_admin(request)
    row = await session.get(SupportGroup, group_id)
    if not row:
        raise HTTPException(404, "Группа не найдена")
    n = (name or "").strip()
    if not n:
        raise HTTPException(400, "Название обязательно")
    row.name = n
    row.room_id = (room_id or "").strip()
    row.timezone = (timezone_name or "").strip() or None
    row.is_active = is_active in ("1", "on", "true")
    return RedirectResponse("/groups", status_code=303)


@router.post("/groups/{group_id}/delete")
async def groups_delete(
    request: Request,
    group_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    require_admin(request)
    row = await session.get(SupportGroup, group_id)
    if row:
        await session.delete(row)
    return RedirectResponse("/groups", status_code=303)
