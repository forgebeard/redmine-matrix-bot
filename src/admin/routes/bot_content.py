"""Bot content and schedule settings managed from admin panel."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import CycleSettings
from database.session import get_session

router = APIRouter(tags=["bot-content"])

_KEYS = {
    "daily_report_enabled": "DAILY_REPORT_ENABLED",
    "daily_report_hour": "DAILY_REPORT_HOUR",
    "daily_report_minute": "DAILY_REPORT_MINUTE",
    "daily_report_html_template": "DAILY_REPORT_HTML_TEMPLATE",
    "daily_report_plain_template": "DAILY_REPORT_PLAIN_TEMPLATE",
}


def _admin() -> object:
    import admin.main as _m

    return _m


def _to_bool_str(value: bool) -> str:
    return "1" if value else "0"


def _safe_hour(value: int) -> int:
    return max(0, min(23, int(value)))


def _safe_minute(value: int) -> int:
    return max(0, min(59, int(value)))


async def _upsert_cycle_setting(session: AsyncSession, key: str, value: str) -> None:
    row = (
        await session.execute(select(CycleSettings).where(CycleSettings.key == key))
    ).scalar_one_or_none()
    if row is None:
        session.add(CycleSettings(key=key, value=value))
    else:
        row.value = value


@router.get("/api/bot/content", response_class=JSONResponse)
async def bot_content_get(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    rows = (await session.execute(select(CycleSettings))).scalars().all()
    by_key = {r.key: r.value for r in rows}
    return {
        "ok": True,
        "settings": {
            "daily_report_enabled": by_key.get(_KEYS["daily_report_enabled"], "1") in ("1", "true", "on"),
            "daily_report_hour": int(by_key.get(_KEYS["daily_report_hour"], "9") or 9),
            "daily_report_minute": int(by_key.get(_KEYS["daily_report_minute"], "0") or 0),
            "daily_report_html_template": by_key.get(_KEYS["daily_report_html_template"], ""),
            "daily_report_plain_template": by_key.get(_KEYS["daily_report_plain_template"], ""),
        },
    }


@router.post("/api/bot/content", response_class=JSONResponse)
async def bot_content_save(
    request: Request,
    daily_report_enabled: Annotated[bool, Form()] = True,
    daily_report_hour: Annotated[int, Form()] = 9,
    daily_report_minute: Annotated[int, Form()] = 0,
    daily_report_html_template: Annotated[str, Form()] = "",
    daily_report_plain_template: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    await _upsert_cycle_setting(
        session, _KEYS["daily_report_enabled"], _to_bool_str(bool(daily_report_enabled))
    )
    await _upsert_cycle_setting(session, _KEYS["daily_report_hour"], str(_safe_hour(daily_report_hour)))
    await _upsert_cycle_setting(
        session, _KEYS["daily_report_minute"], str(_safe_minute(daily_report_minute))
    )
    await _upsert_cycle_setting(
        session, _KEYS["daily_report_html_template"], (daily_report_html_template or "").strip()
    )
    await _upsert_cycle_setting(
        session, _KEYS["daily_report_plain_template"], (daily_report_plain_template or "").strip()
    )
    await session.commit()
    return {"ok": True}
