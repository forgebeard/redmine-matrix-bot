"""Groups routes: /groups."""

from __future__ import annotations

import json
import logging
from datetime import datetime as _dt
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import GroupVersionRoute, StatusRoomRoute, SupportGroup
from database.session import get_session

logger = logging.getLogger("redmine_admin")

router = APIRouter(tags=["groups"])


def _admin() -> object:
    """Late import to avoid circular dependency with main.py."""
    import admin.main as _m

    return _m


# --- Group CRUD ---


@router.get("/groups", response_class=HTMLResponse)
async def groups_list(
    request: Request,
    q: str = "",
    highlight_group_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    q = (q or "").strip()
    stmt = select(SupportGroup)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            admin.or_(SupportGroup.name.ilike(like), SupportGroup.room_id.ilike(like))
        )
    stmt = stmt.order_by(SupportGroup.is_active.desc(), SupportGroup.name.asc())
    _all_groups = list((await session.execute(stmt)).scalars().all())
    rows = [r for r in _all_groups if r.name != admin.GROUP_UNASSIGNED_NAME]
    return admin.templates.TemplateResponse(
        request,
        "panel/groups_list.html",
        {
            "items": rows,
            "q": q,
            "highlight_group_id": highlight_group_id,
            "list_total": len(rows),
        },
    )


@router.get("/groups/new", response_class=HTMLResponse)
async def groups_new(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    notify_catalog, versions_catalog = await admin._load_catalogs(session)
    return admin.templates.TemplateResponse(
        request,
        "panel/group_form.html",
        {
            "title": "Новая группа",
            "g": None,
            "bot_tz": admin.os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "timezone_top_options": admin._top_timezone_options(),
            "timezone_all_options": admin._standard_timezone_options(),
            "timezone_labels": admin._timezone_labels(admin._standard_timezone_options()),
            "status_routes": [],
            "status_err": "",
            "status_msg": "",
            "notify_json": '["all"]',
            "notify_preset": "all",
            "notify_selected": ["all"],
            "notify_catalog": notify_catalog,
            "versions_catalog": versions_catalog,
            "initial_version_keys": "",
            "selected_version_keys": [],
            "version_preset": "all",
        },
    )


@router.post("/groups/test-message")
async def group_test_message(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Отправляет тестовое сообщение в комнату группы по room_id."""
    admin = _admin()
    try:
        admin._verify_csrf_json(request)
    except HTTPException as e:
        return JSONResponse({"ok": False, "error": "Ошибка CSRF токена"}, status_code=e.status_code)

    admin_user = getattr(request.state, "current_user", None)
    if not admin_user or getattr(admin_user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    client = await admin._get_matrix_client(session)
    if not client:
        return JSONResponse({"ok": False, "error": "Matrix не настроен"}, status_code=400)

    try:
        form = await request.form()
        room_id = (form.get("room_id") or "").strip()
    except Exception as e:
        logger.error("Failed to parse form: %s", e)
        await client.close()
        return JSONResponse(
            {"ok": False, "error": "Не удалось прочитать данные формы"}, status_code=400
        )

    if not room_id:
        await client.close()
        return JSONResponse({"ok": False, "error": "Не указан ID комнаты"}, status_code=400)

    from src.matrix_send import room_send_with_retry

    ts = _dt.now().strftime("%H:%M:%S")
    html = (
        f"<b>Тестовое сообщение группы</b><br>"
        f"Это тест от панели управления.<br>"
        f"Если вы это видите — подключение работает!<br>"
        f"<small>Отправлено: {ts}</small>"
    )
    text_plain = f"Тестовое сообщение группы\nЭто тест от панели управления.\nОтправлено: {ts}"

    try:
        logger.info("group_test_message: syncing to find room %s...", room_id)
        if not await admin._sync_matrix_client(client):
            await client.close()
            return JSONResponse(
                {"ok": False, "error": "Не удалось синхронизироваться с Matrix"}, status_code=500
            )

        if room_id not in client.rooms:
            await client.close()
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"Бот не является участником комнаты {room_id}. Пригласите его в Matrix.",
                },
                status_code=400,
            )

        logger.info("group_test_message: sending to %s", room_id)
        content = {
            "msgtype": "m.text",
            "body": text_plain,
            "format": "org.matrix.custom.html",
            "formatted_body": html,
        }
        await room_send_with_retry(client, room_id, content)
        await client.close()
        return JSONResponse({"ok": True})
    except Exception:
        import traceback as _tb

        logger.error("group_test_message_failed room_id=%s\n%s", room_id, _tb.format_exc())
        await client.close()
        return JSONResponse(
            {"ok": False, "error": "Не удалось отправить сообщение. Проверьте логи админки."},
            status_code=500,
        )


@router.get("/groups/{group_id}/edit", response_class=HTMLResponse)
async def groups_edit(
    request: Request,
    group_id: int,
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row:
        raise HTTPException(404, "Группа не найдена")
    if admin._is_reserved_support_group(row):
        raise HTTPException(404, "Группа не найдена")
    status_err = (request.query_params.get("status_err") or "").strip()
    status_msg = (request.query_params.get("status_msg") or "").strip()
    version_err = (request.query_params.get("version_err") or "").strip()
    version_msg = (request.query_params.get("version_msg") or "").strip()
    room = (row.room_id or "").strip()
    sr_stmt = (
        select(StatusRoomRoute)
        .where(StatusRoomRoute.room_id == room)
        .order_by(StatusRoomRoute.status_key)
    )
    status_rows = list((await session.execute(sr_stmt)).scalars().all()) if room else []
    gv_stmt = (
        select(GroupVersionRoute)
        .where(GroupVersionRoute.group_id == group_id)
        .order_by(GroupVersionRoute.version_key)
    )
    version_rows = list((await session.execute(gv_stmt)).scalars().all())
    notify_catalog, versions_catalog = await admin._load_catalogs(session)
    notify_keys = {item["key"] for item in notify_catalog}
    notify_selected = [str(x).strip() for x in (row.notify or ["all"]) if str(x).strip()]
    if "all" not in notify_selected:
        notify_selected = [k for k in notify_selected if k in notify_keys]
    version_set = set(versions_catalog)
    selected_versions = [r.version_key for r in version_rows if r.version_key in version_set]
    return admin.templates.TemplateResponse(
        request,
        "panel/group_form.html",
        {
            "title": "Редактирование группы",
            "g": row,
            "bot_tz": admin.os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "timezone_top_options": admin._top_timezone_options(),
            "timezone_all_options": admin._standard_timezone_options(),
            "timezone_labels": admin._timezone_labels(admin._standard_timezone_options()),
            "status_routes": status_rows,
            "status_err": status_err,
            "status_msg": status_msg,
            "version_routes": version_rows,
            "version_err": version_err,
            "version_msg": version_msg,
            "notify_json": json.dumps(row.notify, ensure_ascii=False),
            "notify_preset": admin._notify_preset(row.notify),
            "notify_selected": notify_selected,
            "notify_catalog": notify_catalog,
            "versions_catalog": versions_catalog,
            "selected_version_keys": selected_versions,
            "version_preset": admin._version_preset(selected_versions, versions_catalog),
        },
    )


@router.post("/groups")
async def groups_create(
    request: Request,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str | None, Form()] = None,
    initial_status_keys: Annotated[str, Form()] = "",
    initial_version_keys: Annotated[str, Form()] = "",
    version_keys_json: Annotated[str, Form()] = "",
    version_preset: Annotated[str, Form()] = "all",
    version_values: Annotated[list[str], Form()] = [],
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
    admin = _admin()
    notify_catalog, versions_catalog = await admin._load_catalogs(session)
    notify_allowed = [item["key"] for item in notify_catalog]
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    n = (name or "").strip()
    if not n:
        raise HTTPException(400, "Название обязательно")
    if n == admin.GROUP_UNASSIGNED_NAME:
        raise HTTPException(400, "Это имя зарезервировано для системы")
    if admin._normalized_group_filter_key(n) == admin._normalized_group_filter_key(
        admin.GROUP_USERS_FILTER_ALL_LABEL
    ):
        raise HTTPException(400, "Это имя зарезервировано для фильтра списка пользователей")
    existing_name = await session.execute(
        select(SupportGroup.id).where(SupportGroup.name == n).limit(1)
    )
    if existing_name.scalar_one_or_none() is not None:
        raise HTTPException(400, "Группа с таким названием уже существует")
    room = (room_id or "").strip()
    if not room:
        raise HTTPException(400, "Укажите ID комнаты группы")
    status_keys = admin._parse_status_keys_list(initial_status_keys)
    if work_hours_from and work_hours_to:
        wh = f"{work_hours_from.strip()}-{work_hours_to.strip()}"
    else:
        wh = work_hours.strip() or None
    if work_days_values:
        wd = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        wd = admin._parse_work_days(work_days_json)
    if notify_preset == "all":
        notify = ["all"]
    elif notify_preset == "new_only":
        notify = ["new"]
    elif notify_preset == "overdue_only":
        notify = ["overdue"]
    elif notify_preset == "custom":
        notify = admin._normalize_notify(notify_values, notify_allowed)
    else:
        notify = admin._parse_notify(notify_json)
    row = SupportGroup(
        name=n,
        room_id=room,
        timezone=(timezone_name or "").strip() or None,
        is_active=True if is_active is None else is_active in ("1", "on", "true"),
        notify=notify,
        work_hours=wh,
        work_days=wd,
        dnd=dnd in ("1", "on", "true"),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(400, "Не удалось создать группу: проверьте уникальность названия")
    rid = row.id
    for key in status_keys:
        ex = await session.execute(
            select(StatusRoomRoute.id).where(StatusRoomRoute.status_key == key)
        )
        if ex.scalar_one_or_none():
            continue
        session.add(StatusRoomRoute(status_key=key, room_id=room))
    version_keys = admin._parse_json_string_list(
        version_keys_json
    ) or admin._parse_status_keys_list(initial_version_keys)
    if version_preset == "all":
        version_keys = list(versions_catalog)
    elif version_preset == "custom":
        version_keys = admin._normalize_versions(version_values, versions_catalog)
    for vkey in version_keys:
        ex = await session.execute(
            select(GroupVersionRoute.id).where(
                GroupVersionRoute.group_id == rid,
                GroupVersionRoute.version_key == vkey,
            )
        )
        if ex.scalar_one_or_none():
            continue
        session.add(GroupVersionRoute(group_id=rid, version_key=vkey, room_id=room))
    await admin._maybe_log_admin_crud(
        session,
        user,
        "group",
        "create",
        {"id": rid, "name": n},
    )
    return RedirectResponse(f"/groups?highlight_group_id={rid}", status_code=303)


@router.post("/groups/{group_id}")
async def groups_update(
    request: Request,
    group_id: int,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str | None, Form()] = None,
    notify_json: Annotated[str, Form()] = "",
    notify_preset: Annotated[str, Form()] = "all",
    notify_values: Annotated[list[str], Form()] = [],
    version_preset: Annotated[str, Form()] = "all",
    version_values: Annotated[list[str], Form()] = [],
    version_keys_json: Annotated[str, Form()] = "",
    initial_version_keys: Annotated[str, Form()] = "",
    work_hours: Annotated[str, Form()] = "",
    work_hours_from: Annotated[str, Form()] = "",
    work_hours_to: Annotated[str, Form()] = "",
    work_days_json: Annotated[str, Form()] = "",
    work_days_values: Annotated[list[str], Form()] = [],
    dnd: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    notify_catalog, versions_catalog = await admin._load_catalogs(session)
    notify_allowed = [item["key"] for item in notify_catalog]
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row:
        raise HTTPException(404, "Группа не найдена")
    if admin._is_reserved_support_group(row):
        raise HTTPException(403, "Системную группу нельзя менять")
    n = (name or "").strip()
    if not n:
        raise HTTPException(400, "Название обязательно")
    if n == admin.GROUP_UNASSIGNED_NAME:
        raise HTTPException(400, "Это имя зарезервировано для системы")
    if admin._normalized_group_filter_key(n) == admin._normalized_group_filter_key(
        admin.GROUP_USERS_FILTER_ALL_LABEL
    ):
        raise HTTPException(400, "Это имя зарезервировано для фильтра списка пользователей")
    existing_name = await session.execute(
        select(SupportGroup.id).where(SupportGroup.name == n, SupportGroup.id != group_id).limit(1)
    )
    if existing_name.scalar_one_or_none() is not None:
        raise HTTPException(400, "Группа с таким названием уже существует")
    if work_hours_from and work_hours_to:
        wh = f"{work_hours_from.strip()}-{work_hours_to.strip()}"
    else:
        wh = work_hours.strip() or None
    if work_days_values:
        wd = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        wd = admin._parse_work_days(work_days_json)
    if notify_preset == "all":
        notify = ["all"]
    elif notify_preset == "new_only":
        notify = ["new"]
    elif notify_preset == "overdue_only":
        notify = ["overdue"]
    elif notify_preset == "custom":
        notify = admin._normalize_notify(notify_values, notify_allowed)
    else:
        notify = admin._parse_notify(notify_json)
    old_room = (row.room_id or "").strip()
    new_room = (room_id or "").strip()
    row.name = n
    row.room_id = new_room
    row.timezone = (timezone_name or "").strip() or None
    if is_active is not None:
        row.is_active = is_active in ("1", "on", "true")
    row.notify = notify
    row.work_hours = wh
    row.work_days = wd
    row.dnd = dnd in ("1", "on", "true")
    if version_preset == "all":
        submitted_versions = list(versions_catalog)
    elif version_preset == "custom":
        submitted_versions = admin._normalize_versions(version_values, versions_catalog)
    else:
        submitted_versions = admin._parse_json_string_list(
            version_keys_json
        ) or admin._parse_status_keys_list(initial_version_keys)
    existing_routes = list(
        (
            await session.execute(
                select(GroupVersionRoute).where(GroupVersionRoute.group_id == group_id)
            )
        )
        .scalars()
        .all()
    )
    existing_by_key = {r.version_key: r for r in existing_routes}
    submitted_set = set(submitted_versions)
    for r in existing_routes:
        if r.version_key not in submitted_set:
            await session.delete(r)
    for key in submitted_versions:
        ex = existing_by_key.get(key)
        if ex:
            ex.room_id = new_room
            continue
        session.add(GroupVersionRoute(group_id=group_id, version_key=key, room_id=new_room))
    if old_room and new_room and old_room != new_room:
        await session.execute(
            admin.update(StatusRoomRoute)
            .where(StatusRoomRoute.room_id == old_room)
            .values(room_id=new_room)
        )
        await session.execute(
            admin.update(GroupVersionRoute)
            .where(GroupVersionRoute.group_id == group_id, GroupVersionRoute.room_id == old_room)
            .values(room_id=new_room)
        )
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(400, "Не удалось сохранить группу: проверьте уникальность названия")
    await admin._maybe_log_admin_crud(
        session,
        user,
        "group",
        "update",
        {"id": group_id, "name": n},
    )
    return RedirectResponse(f"/groups?highlight_group_id={group_id}", status_code=303)


@router.post("/groups/{group_id}/status-routes/add")
async def group_status_route_add(
    request: Request,
    group_id: int,
    status_key: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row or admin._is_reserved_support_group(row):
        raise HTTPException(404, "Группа не найдена")
    room = (row.room_id or "").strip()
    if not room:
        return RedirectResponse(f"/groups/{group_id}/edit?status_err=no_room", status_code=303)
    key = (status_key or "").strip()
    if not key:
        return RedirectResponse(f"/groups/{group_id}/edit?status_err=empty", status_code=303)
    exists = await session.execute(select(StatusRoomRoute).where(StatusRoomRoute.status_key == key))
    if exists.scalar_one_or_none():
        return RedirectResponse(f"/groups/{group_id}/edit?status_err=exists", status_code=303)
    session.add(StatusRoomRoute(status_key=key, room_id=room))
    await admin._maybe_log_admin_crud(
        session,
        user,
        "group_status_route",
        "create",
        {"group_id": group_id, "status_key": key},
    )
    return RedirectResponse(f"/groups/{group_id}/edit?status_msg=added", status_code=303)


@router.post("/groups/{group_id}/status-routes/{route_row_id}/delete")
async def group_status_route_delete(
    request: Request,
    group_id: int,
    route_row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row or admin._is_reserved_support_group(row):
        raise HTTPException(404, "Группа не найдена")
    room = (row.room_id or "").strip()
    rte = await session.get(StatusRoomRoute, route_row_id)
    if not rte or (rte.room_id or "").strip() != room:
        raise HTTPException(404, "Маршрут не найден")
    sk = rte.status_key
    await session.delete(rte)
    await admin._maybe_log_admin_crud(
        session,
        user,
        "group_status_route",
        "delete",
        {"group_id": group_id, "status_key": sk, "route_id": route_row_id},
    )
    return RedirectResponse(f"/groups/{group_id}/edit?status_msg=deleted", status_code=303)


@router.post("/groups/{group_id}/version-routes/add")
async def group_version_route_add(
    request: Request,
    group_id: int,
    version_key: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row or admin._is_reserved_support_group(row):
        raise HTTPException(404, "Группа не найдена")
    room = (row.room_id or "").strip()
    if not room:
        return RedirectResponse(f"/groups/{group_id}/edit?version_err=no_room", status_code=303)
    key = (version_key or "").strip()
    if not key:
        return RedirectResponse(f"/groups/{group_id}/edit?version_err=empty", status_code=303)
    exists = await session.execute(
        select(GroupVersionRoute.id).where(
            GroupVersionRoute.group_id == group_id,
            GroupVersionRoute.version_key == key,
        )
    )
    if exists.scalar_one_or_none():
        return RedirectResponse(f"/groups/{group_id}/edit?version_err=exists", status_code=303)
    session.add(GroupVersionRoute(group_id=group_id, version_key=key, room_id=room))
    await admin._maybe_log_admin_crud(
        session,
        user,
        "group_version_route",
        "create",
        {"group_id": group_id, "version_key": key},
    )
    return RedirectResponse(f"/groups/{group_id}/edit?version_msg=added", status_code=303)


@router.post("/groups/{group_id}/version-routes/{route_row_id}/delete")
async def group_version_route_delete(
    request: Request,
    group_id: int,
    route_row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if not row or admin._is_reserved_support_group(row):
        raise HTTPException(404, "Группа не найдена")
    rte = await session.get(GroupVersionRoute, route_row_id)
    if not rte or rte.group_id != group_id:
        raise HTTPException(404, "Маршрут не найден")
    vkey = rte.version_key
    await session.delete(rte)
    await admin._maybe_log_admin_crud(
        session,
        user,
        "group_version_route",
        "delete",
        {"group_id": group_id, "version_key": vkey, "route_id": route_row_id},
    )
    return RedirectResponse(f"/groups/{group_id}/edit?version_msg=deleted", status_code=303)


@router.post("/groups/{group_id}/delete")
async def groups_delete(
    request: Request,
    group_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(SupportGroup, group_id)
    if row:
        if admin._is_reserved_support_group(row):
            raise HTTPException(403, "Системную группу нельзя удалить")
        gid, gname = row.id, row.name
        await session.delete(row)
        await admin._maybe_log_admin_crud(
            session, user, "group", "delete", {"id": gid, "name": gname}
        )
    return RedirectResponse("/groups", status_code=303)
