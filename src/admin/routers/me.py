"""Самообслуживание: настройки уведомлений для текущего пользователя."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.constants import COOKIE_SECURE, CSRF_COOKIE_NAME
from admin.csrf import ensure_csrf as _ensure_csrf, verify_csrf as _verify_csrf
from admin.notify_prefs import normalize_notify, notify_preset, parse_notify, parse_work_days, parse_work_hours_range
from admin.templates_env import templates
from database.models import BotUser
from database.session import get_session

router = APIRouter()


@router.get("/me/settings", response_class=HTMLResponse)
async def me_settings_get(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)

    redmine_id = getattr(user, "redmine_id", None)
    csrf_token, set_cookie = _ensure_csrf(request)
    if redmine_id is None:
        resp = templates.TemplateResponse(
            request,
            "my_settings.html",
            {
                "room": None,
                "notify_json": '["all"]',
                "notify_preset": "all",
                "notify_selected": ["all"],
                "work_hours": "",
                "work_hours_from": "",
                "work_hours_to": "",
                "work_days_json": "",
                "work_days_selected": [0, 1, 2, 3, 4],
                "dnd": False,
                "error": "Сначала привяжите комнату через Matrix binding.",
                "csrf_token": csrf_token,
            },
            status_code=400,
        )
        if set_cookie:
            resp.set_cookie(CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax")
        return resp

    r = await session.execute(select(BotUser).where(BotUser.redmine_id == redmine_id))
    bot_user = r.scalar_one_or_none()
    if not bot_user:
        raise HTTPException(404, "BotUser не найден")

    resp = templates.TemplateResponse(
        request,
        "my_settings.html",
        {
            "room": bot_user.room,
            "notify_json": json.dumps(bot_user.notify, ensure_ascii=False)
            if bot_user.notify is not None
            else '["all"]',
            "notify_preset": notify_preset(bot_user.notify),
            "notify_selected": bot_user.notify or ["all"],
            "work_hours": bot_user.work_hours or "",
            "work_hours_from": parse_work_hours_range(bot_user.work_hours or "")[0],
            "work_hours_to": parse_work_hours_range(bot_user.work_hours or "")[1],
            "work_days_json": json.dumps(bot_user.work_days, ensure_ascii=False)
            if bot_user.work_days is not None
            else "",
            "work_days_selected": bot_user.work_days if bot_user.work_days is not None else [0, 1, 2, 3, 4],
            "dnd": bool(bot_user.dnd),
            "error": None,
            "csrf_token": csrf_token,
        },
    )
    if set_cookie:
        resp.set_cookie(CSRF_COOKIE_NAME, csrf_token, httponly=True, secure=COOKIE_SECURE, samesite="lax")
    return resp


@router.post("/me/settings")
async def me_settings_post(
    request: Request,
    notify_json: Annotated[str, Form()] = "",
    notify_preset_param: Annotated[str, Form(alias="notify_preset")] = "all",
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
    user = getattr(request.state, "current_user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)

    redmine_id = getattr(user, "redmine_id", None)
    if redmine_id is None:
        raise HTTPException(400, "Сначала привяжите комнату через Matrix binding.")

    r = await session.execute(select(BotUser).where(BotUser.redmine_id == redmine_id))
    bot_user = r.scalar_one_or_none()
    if not bot_user:
        raise HTTPException(404, "BotUser не найден")

    preset = notify_preset_param
    if preset == "all":
        bot_user.notify = ["all"]
    elif preset == "new_only":
        bot_user.notify = ["new"]
    elif preset == "overdue_only":
        bot_user.notify = ["overdue"]
    elif preset == "custom":
        bot_user.notify = normalize_notify(notify_values)
    else:
        bot_user.notify = parse_notify(notify_json)
    if work_hours_from and work_hours_to:
        bot_user.work_hours = f"{work_hours_from.strip()}-{work_hours_to.strip()}"
    else:
        bot_user.work_hours = work_hours.strip() or None
    if work_days_values:
        bot_user.work_days = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        bot_user.work_days = parse_work_days(work_days_json)
    bot_user.dnd = dnd in ("on", "true", "1")
    await session.flush()

    return RedirectResponse("/me/settings", status_code=303)
