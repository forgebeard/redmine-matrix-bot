"""Маршруты Matrix по статусу и версии Redmine."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.authz import require_admin
from admin.csrf import verify_csrf as _verify_csrf
from admin.templates_env import templates
from database.models import StatusRoomRoute, VersionRoomRoute
from database.session import get_session

router = APIRouter()


@router.get("/routes/status", response_class=HTMLResponse)
async def routes_status(
    request: Request,
    q: str = "",
    added: int = 0,
    skipped: int = 0,
    error: str = "",
    session: AsyncSession = Depends(get_session),
):
    require_admin(request)
    stmt = select(StatusRoomRoute)
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                StatusRoomRoute.status_key.ilike(like),
                StatusRoomRoute.room_id.ilike(like),
            )
        )
    stmt = stmt.order_by(StatusRoomRoute.status_key)
    r = await session.execute(stmt)
    rows = list(r.scalars().all())
    room_map: dict[str, list[str]] = {}
    for row in rows:
        room_map.setdefault(row.room_id, []).append(row.status_key)
    room_map = {k: sorted(v) for k, v in sorted(room_map.items(), key=lambda x: x[0])}
    return templates.TemplateResponse(
        request,
        "routes_status.html",
        {"rows": rows, "room_map": room_map, "added": added, "skipped": skipped, "error": error, "q": q},
    )


@router.post("/routes/status")
async def routes_status_add(
    request: Request,
    status_key: Annotated[str, Form()],
    room_id: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    require_admin(request)
    key = status_key.strip()
    room = room_id.strip()
    if not key or not room:
        return RedirectResponse("/routes/status?error=Заполните+оба+поля", status_code=303)
    exists = await session.execute(select(StatusRoomRoute).where(StatusRoomRoute.status_key == key))
    if exists.scalar_one_or_none():
        return RedirectResponse("/routes/status?added=0&skipped=1", status_code=303)
    session.add(StatusRoomRoute(status_key=key, room_id=room))
    return RedirectResponse("/routes/status?added=1&skipped=0", status_code=303)


@router.post("/routes/status/by-room")
async def routes_status_add_by_room(
    request: Request,
    room_id: Annotated[str, Form()],
    status_keys: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    require_admin(request)
    room = room_id.strip()
    raw_statuses = status_keys.strip()
    if not room or not raw_statuses:
        raise HTTPException(400, "Комната и статусы обязательны")
    parts = [p.strip() for p in raw_statuses.replace("\n", ",").split(",")]
    statuses = [p for p in parts if p]
    existing_q = await session.execute(select(StatusRoomRoute.status_key))
    existing = {s[0] for s in existing_q.all()}
    added = 0
    skipped = 0
    for key in statuses:
        if key in existing:
            skipped += 1
            continue
        session.add(StatusRoomRoute(status_key=key, room_id=room))
        existing.add(key)
        added += 1
    return RedirectResponse(f"/routes/status?added={added}&skipped={skipped}", status_code=303)


@router.post("/routes/status/{row_id}/delete")
async def routes_status_del(
    request: Request,
    row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    require_admin(request)
    await session.execute(delete(StatusRoomRoute).where(StatusRoomRoute.id == row_id))
    return RedirectResponse("/routes/status", status_code=303)


@router.get("/routes/version", response_class=HTMLResponse)
async def routes_version(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    require_admin(request)
    r = await session.execute(select(VersionRoomRoute).order_by(VersionRoomRoute.version_key))
    rows = list(r.scalars().all())
    return templates.TemplateResponse(
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
    _verify_csrf(request, csrf_token)
    require_admin(request)
    session.add(VersionRoomRoute(version_key=version_key.strip(), room_id=room_id.strip()))
    return RedirectResponse("/routes/version", status_code=303)


@router.post("/routes/version/{row_id}/delete")
async def routes_version_del(
    request: Request,
    row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    _verify_csrf(request, csrf_token)
    require_admin(request)
    await session.execute(delete(VersionRoomRoute).where(VersionRoomRoute.id == row_id))
    return RedirectResponse("/routes/version", status_code=303)
