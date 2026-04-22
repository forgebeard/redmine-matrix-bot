"""Routes management: legacy GET redirect for /routes/status + global version routes (canonical /settings)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import VersionRoomRoute
from database.session import get_session

router = APIRouter(tags=["routes_mgmt"])

# Канонический URL глобальных маршрутов версий (Act 4).
_CANON_VERSION_ROUTES = "/settings/routes/version"
_LEGACY_VERSION_POST_GONE = (
    "Маршрут перенесён. Используйте формы на "
    + _CANON_VERSION_ROUTES
    + " (POST только на новый путь)."
)


def _admin() -> object:
    """Late import to avoid circular dependency with main.py."""
    import admin.main as _m

    return _m


def _require_admin(request: Request) -> object:
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    return user


async def _routes_version_html(request: Request, session: AsyncSession):
    _require_admin(request)
    admin = _admin()
    r = await session.execute(select(VersionRoomRoute).order_by(VersionRoomRoute.version_key))
    rows = list(r.scalars().all())
    return admin.templates.TemplateResponse(
        request,
        "panel/routes_version.html",
        {"rows": rows},
    )


async def _routes_version_add(
    request: Request,
    session: AsyncSession,
    version_key: str,
    room_id: str,
    csrf_token: str,
) -> RedirectResponse:
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = _require_admin(request)
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
    return RedirectResponse(_CANON_VERSION_ROUTES, status_code=303)


async def _routes_version_delete(
    request: Request,
    session: AsyncSession,
    row_id: int,
    csrf_token: str,
) -> RedirectResponse:
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = _require_admin(request)
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
    return RedirectResponse(_CANON_VERSION_ROUTES, status_code=303)


# ── Status routes (legacy GET only) ──────────────────────────────────────────


@router.get("/routes/status")
async def routes_status_legacy_redirect():
    """Старый URL: маршруты статусов настраиваются в карточке группы (`/groups/{id}/status-routes/*`)."""
    return RedirectResponse("/groups", status_code=303)


# ── Version routes (canonical under /settings) ───────────────────────────────


@router.get("/settings/routes/version", response_class=HTMLResponse)
async def settings_routes_version_get(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    return await _routes_version_html(request, session)


@router.post("/settings/routes/version")
async def settings_routes_version_post(
    request: Request,
    version_key: Annotated[str, Form()],
    room_id: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    return await _routes_version_add(request, session, version_key, room_id, csrf_token)


@router.post("/settings/routes/version/{row_id}/delete")
async def settings_routes_version_delete(
    request: Request,
    row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    return await _routes_version_delete(request, session, row_id, csrf_token)


@router.get("/routes/version")
async def routes_version_legacy_redirect_to_canonical():
    """Старый URL: постоянный редирект на канонический путь (Act 4)."""
    return RedirectResponse(_CANON_VERSION_ROUTES, status_code=301)


@router.post("/routes/version")
async def routes_version_post_legacy_gone():
    raise HTTPException(status_code=410, detail=_LEGACY_VERSION_POST_GONE)


@router.post("/routes/version/{row_id}/delete")
async def routes_version_delete_legacy_gone(row_id: int):
    del row_id
    raise HTTPException(status_code=410, detail=_LEGACY_VERSION_POST_GONE)
