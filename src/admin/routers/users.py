"""Пользователи бота (BotUser): список, формы, CRUD."""

from __future__ import annotations

import json
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.authz import require_admin
from admin.constants import GROUP_UNASSIGNED_NAME
from admin.csrf import verify_csrf as _verify_csrf
from admin.notify_prefs import normalize_notify, notify_preset, parse_notify, parse_work_days
from admin.templates_env import templates
from database.models import BotUser, SupportGroup
from database.session import get_session

router = APIRouter()


@router.get("/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    q: str = "",
    group_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    require_admin(request)

    groups_rows = list((await session.execute(select(SupportGroup).order_by(SupportGroup.name.asc()))).scalars().all())
    groups_by_id = {g.id: g for g in groups_rows}

    stmt = select(BotUser)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                BotUser.display_name.ilike(like),
                BotUser.department.ilike(like),
                BotUser.room.ilike(like),
            )
        )
    if group_id is not None:
        if group_id == -1:
            stmt = stmt.where(BotUser.group_id.is_(None))
        else:
            stmt = stmt.where(BotUser.group_id == group_id)
    stmt = stmt.order_by(BotUser.group_id.asc().nulls_last(), BotUser.display_name.asc().nulls_last(), BotUser.redmine_id)
    rows = list((await session.execute(stmt)).scalars().all())

    grouped: dict[str, list[BotUser]] = {}
    for row in rows:
        if row.group_id is None:
            key = GROUP_UNASSIGNED_NAME
        else:
            key = groups_by_id.get(row.group_id).name if groups_by_id.get(row.group_id) else GROUP_UNASSIGNED_NAME
        grouped.setdefault(key, []).append(row)

    return templates.TemplateResponse(
        request,
        "users_list.html",
        {
            "users": rows,
            "grouped_users": grouped,
            "groups": groups_rows,
            "groups_by_id": groups_by_id,
            "q": q,
            "group_filter": group_id,
            "group_unassigned_name": GROUP_UNASSIGNED_NAME,
        },
    )


@router.get("/users/new", response_class=HTMLResponse)
async def users_new(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    require_admin(request)
    groups_rows = list((await session.execute(select(SupportGroup).order_by(SupportGroup.name.asc()))).scalars().all())
    return templates.TemplateResponse(
        request,
        "user_form.html",
        {
            "title": "Новый пользователь",
            "u": None,
            "notify_json": '["all"]',
            "notify_preset": "all",
            "notify_selected": ["all"],
            "groups": groups_rows,
            "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
        },
    )


@router.post("/users")
async def users_create(
    request: Request,
    redmine_id: Annotated[int, Form()],
    room: Annotated[str, Form()],
    display_name: Annotated[str, Form()] = "",
    group_id: Annotated[str, Form()] = "",
    notify_json: Annotated[str, Form()] = "",
    notify_preset: Annotated[str, Form()] = "all",
    notify_values: Annotated[list[str], Form()] = [],
    work_hours: Annotated[str, Form()] = "",
    work_hours_from: Annotated[str, Form()] = "",
    work_hours_to: Annotated[str, Form()] = "",
    work_days_json: Annotated[str, Form()] = "",
    work_days_values: Annotated[list[str], Form()] = [],
    dnd: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    require_admin(request)
    if work_hours_from and work_hours_to:
        wh = f"{work_hours_from.strip()}-{work_hours_to.strip()}"
    else:
        wh = work_hours.strip() or None
    if work_days_values:
        wd = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        wd = parse_work_days(work_days_json)
    if notify_preset == "all":
        notify = ["all"]
    elif notify_preset == "new_only":
        notify = ["new"]
    elif notify_preset == "overdue_only":
        notify = ["overdue"]
    elif notify_preset == "custom":
        notify = normalize_notify(notify_values)
    else:
        notify = parse_notify(notify_json)
    row = BotUser(
        redmine_id=redmine_id,
        display_name=display_name.strip() or None,
        group_id=int(group_id) if str(group_id).isdigit() else None,
        department=None,
        room=room.strip(),
        notify=notify,
        work_hours=wh,
        work_days=wd,
        dnd=dnd in ("on", "true", "1"),
    )
    session.add(row)
    await session.flush()
    return RedirectResponse("/users", status_code=303)


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def users_edit(
    request: Request,
    user_id: int,
    session: AsyncSession = Depends(get_session),
):
    require_admin(request)
    row = await session.get(BotUser, user_id)
    if not row:
        raise HTTPException(404)
    groups_rows = list((await session.execute(select(SupportGroup).order_by(SupportGroup.name.asc()))).scalars().all())
    return templates.TemplateResponse(
        request,
        "user_form.html",
        {
            "title": f"Пользователь Redmine {row.redmine_id}",
            "u": row,
            "notify_json": json.dumps(row.notify, ensure_ascii=False),
            "notify_preset": notify_preset(row.notify),
            "notify_selected": row.notify or ["all"],
            "groups": groups_rows,
            "bot_tz": os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
        },
    )


@router.post("/users/{user_id}")
async def users_update(
    request: Request,
    user_id: int,
    redmine_id: Annotated[int, Form()],
    room: Annotated[str, Form()],
    display_name: Annotated[str, Form()] = "",
    group_id: Annotated[str, Form()] = "",
    notify_json: Annotated[str, Form()] = "",
    notify_preset: Annotated[str, Form()] = "all",
    notify_values: Annotated[list[str], Form()] = [],
    work_hours: Annotated[str, Form()] = "",
    work_hours_from: Annotated[str, Form()] = "",
    work_hours_to: Annotated[str, Form()] = "",
    work_days_json: Annotated[str, Form()] = "",
    work_days_values: Annotated[list[str], Form()] = [],
    dnd: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    require_admin(request)
    row = await session.get(BotUser, user_id)
    if not row:
        raise HTTPException(404)
    row.redmine_id = redmine_id
    row.display_name = display_name.strip() or None
    row.group_id = int(group_id) if str(group_id).isdigit() else None
    row.room = room.strip()
    if notify_preset == "all":
        row.notify = ["all"]
    elif notify_preset == "new_only":
        row.notify = ["new"]
    elif notify_preset == "overdue_only":
        row.notify = ["overdue"]
    elif notify_preset == "custom":
        row.notify = normalize_notify(notify_values)
    else:
        row.notify = parse_notify(notify_json)
    if work_hours_from and work_hours_to:
        row.work_hours = f"{work_hours_from.strip()}-{work_hours_to.strip()}"
    else:
        row.work_hours = work_hours.strip() or None
    if work_days_values:
        row.work_days = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        row.work_days = parse_work_days(work_days_json)
    row.dnd = dnd in ("on", "true", "1")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def users_delete(
    request: Request,
    user_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    require_admin(request)
    row = await session.get(BotUser, user_id)
    if row:
        await session.delete(row)
    return RedirectResponse("/users", status_code=303)
