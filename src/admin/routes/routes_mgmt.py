"""Legacy routes management: /routes/status/*, /routes/version/*."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import StatusRoomRoute, VersionRoomRoute
from database.session import get_session

router = APIRouter(tags=["routes_mgmt"])


def _admin() -> object:
    """Late import to avoid circular dependency with main.py."""
    import admin.main as _m
    return _m


# ── Status routes (legacy) ───────────────────────────────────────────────────

@router.get("/routes/status")
async def routes_status_legacy_redirect():
    """Старый URL: маршруты статусов настраиваются в карточке группы."""
    return RedirectResponse("/groups", status_code=303)


@router.post("/routes/status")
async def routes_status_add(
    request: Request,
    status_key: Annotated[str, Form()],
    room_id: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    key = status_key.strip()
    room = room_id.strip()
    if not key or not room:
        return RedirectResponse("/groups", status_code=303)
    exists = await session.execute(select(StatusRoomRoute).where(StatusRoomRoute.status_key == key))
    if exists.scalar_one_or_none():
        return RedirectResponse("/groups", status_code=303)
    session.add(StatusRoomRoute(status_key=key, room_id=room))
    return RedirectResponse("/groups", status_code=303)


@router.post("/routes/status/by-room")
async def routes_status_add_by_room(
    request: Request,
    room_id: Annotated[str, Form()],
    status_keys: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    room = room_id.strip()
    raw_statuses = status_keys.strip()
    if not room or not raw_statuses:
        raise HTTPException(400, "Комната и статусы обязательны")
    parts = [p.strip() for p in raw_statuses.replace("\n", ",").split(",")]
    statuses = [p for p in parts if p]
    existing_q = await session.execute(select(StatusRoomRoute.status_key))
    existing = {s[0] for s in existing_q.all()}
    for key in statuses:
        if key in existing:
            continue
        session.add(StatusRoomRoute(status_key=key, room_id=room))
        existing.add(key)
    return RedirectResponse("/groups", status_code=303)


@router.post("/routes/status/{row_id}/delete")
async def routes_status_del(
    request: Request,
    row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    await session.execute(delete(StatusRoomRoute).where(StatusRoomRoute.id == row_id))
    return RedirectResponse("/groups", status_code=303)


# ── Version routes (global) ──────────────────────────────────────────────────

@router.get("/routes/version", response_class=HTMLResponse)
async def routes_version(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    r = await session.execute(select(VersionRoomRoute).order_by(VersionRoomRoute.version_key))
    rows = list(r.scalars().all())
    return admin.templates.TemplateResponse(
        request,
        "routes_version.html",
        {"rows": rows},
    )


@router.post("/routes/version")
async def routes_version_add(
    request: Request,
    version_key: Annotated[str, Form()],
    room_id: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    vr = VersionRoomRoute(version_key=version_key.strip(), room_id=room_id.strip())
    session.add(vr)
    await session.flush()
    await admin._maybe_log_admin_crud(
        session,
        user,
        "route/version_global",
        "create",
        {"id": vr.id, "version_key": vr.version_key},
    )
    return RedirectResponse("/routes/version", status_code=303)


@router.post("/routes/version/{row_id}/delete")
async def routes_version_del(
    request: Request,
    row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    vr = await session.get(VersionRoomRoute, row_id)
    vkey = vr.version_key if vr else ""
    await session.execute(delete(VersionRoomRoute).where(VersionRoomRoute.id == row_id))
    await admin._maybe_log_admin_crud(
        session,
        user,
        "route/version_global",
        "delete",
        {"id": row_id, "version_key": vkey},
    )
    return RedirectResponse("/routes/version", status_code=303)
