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

_TIME_RE = __import__("re").compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _validate_work_time(val: str, label: str) -> str:
    val = (val or "").strip()
    if not val:
        raise HTTPException(400, f"{label}: обязательное поле")
    if not _TIME_RE.match(val):
        raise HTTPException(400, f"{label}: неверный формат (ожидается HH:MM, например 09:00)")
    return val


def _pick_form_values(primary_values: list[str] | None, fallback_json: str) -> list[str]:
    """Use checkbox list first, fallback to hidden JSON list."""
    selected = [str(v).strip() for v in (primary_values or []) if str(v).strip()]
    if selected:
        return selected
    return _admin()._parse_json_string_list(fallback_json)


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

    statuses_catalog = await admin._load_statuses_catalog(session)
    versions_catalog = await admin._load_versions_catalog(session)
    priorities_catalog = await admin._load_priorities_catalog(session)

    status_default_keys = [item["key"] for item in statuses_catalog if item.get("is_default")]
    version_default_keys = [item["key"] for item in versions_catalog if item.get("is_default")]
    priority_default_keys = [item["key"] for item in priorities_catalog if item.get("is_default")]

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
            # Статусы
            "status_json": json.dumps(status_default_keys, ensure_ascii=False),
            "status_preset": "default",
            "status_selected": status_default_keys,
            "statuses_catalog": statuses_catalog,
            # Версии
            "version_json": json.dumps(version_default_keys, ensure_ascii=False),
            "version_preset": "default",
            "version_selected": version_default_keys,
            "versions_catalog": versions_catalog,
            # Приоритеты
            "priority_json": json.dumps(priority_default_keys, ensure_ascii=False),
            "priority_preset": "default",
            "priority_selected": priority_default_keys,
            "priorities_catalog": priorities_catalog,
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

    # Загружаем каталоги (формат [{key, label, is_default}, ...])
    statuses_catalog = await admin._load_statuses_catalog(session)
    versions_catalog = await admin._load_versions_catalog(session)
    priorities_catalog = await admin._load_priorities_catalog(session)

    # ── Статусы ──
    status_keys = {item["key"] for item in statuses_catalog}
    status_default_keys = [item["key"] for item in statuses_catalog if item.get("is_default")]
    notify_selected = [str(x).strip() for x in (row.notify or ["all"]) if str(x).strip()]
    preset = admin._status_preset(row.notify)
    if preset == "default":
        status_selected = status_default_keys
    else:
        status_selected = [k for k in notify_selected if k in status_keys]

    # ── Версии ──
    version_default_keys = [item["key"] for item in versions_catalog if item.get("is_default")]
    version_selected = row.versions or []
    version_preset = "default" if (not version_selected or version_selected == ["all"]) else "custom"
    if version_preset == "default":
        version_selected = version_default_keys

    # ── Приоритеты ──
    priority_default_keys = [item["key"] for item in priorities_catalog if item.get("is_default")]
    priority_selected = row.priorities or []
    priority_preset = "default" if (not priority_selected or priority_selected == ["all"]) else "custom"
    if priority_preset == "default":
        priority_selected = priority_default_keys

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
            # Статусы
            "status_json": json.dumps(row.notify, ensure_ascii=False),
            "status_preset": preset,
            "status_selected": status_selected,
            "statuses_catalog": statuses_catalog,
            # Версии
            "version_json": json.dumps(row.versions or [], ensure_ascii=False),
            "version_preset": version_preset,
            "version_selected": version_selected,
            "versions_catalog": versions_catalog,
            # Приоритеты
            "priority_json": json.dumps(row.priorities or [], ensure_ascii=False),
            "priority_preset": priority_preset,
            "priority_selected": priority_selected,
            "priorities_catalog": priorities_catalog,
        },
    )


@router.post("/groups")
async def groups_create(
    request: Request,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str | None, Form()] = None,
    status_json: Annotated[str, Form()] = "",
    status_preset: Annotated[str, Form()] = "default",
    status_values: Annotated[list[str], Form()] = [],
    version_json: Annotated[str, Form()] = "",
    version_preset: Annotated[str, Form()] = "default",
    version_values: Annotated[list[str], Form()] = [],
    priority_json: Annotated[str, Form()] = "",
    priority_preset: Annotated[str, Form()] = "default",
    priority_values: Annotated[list[str], Form()] = [],
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
    statuses_catalog = await admin._load_statuses_catalog(session)
    versions_catalog = await admin._load_versions_catalog(session)
    priorities_catalog = await admin._load_priorities_catalog(session)
    status_allowed = [item["key"] for item in statuses_catalog]
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

    # Статусы
    if status_preset == "default":
        notify = [item["key"] for item in statuses_catalog if item.get("is_default")]
    elif status_preset == "custom":
        selected_statuses = _pick_form_values(status_values, status_json)
        notify = admin._normalize_notify(selected_statuses, status_allowed)
    else:
        notify = admin._parse_notify(status_json)

    # Версии
    version_catalog_keys = [item["key"] for item in versions_catalog]
    if version_preset == "default":
        versions = [item["key"] for item in versions_catalog if item.get("is_default")]
    elif version_preset == "custom":
        selected_versions = _pick_form_values(version_values, version_json)
        versions = admin._normalize_versions(selected_versions, version_catalog_keys)
    else:
        versions = admin._parse_json_string_list(version_json) or ["all"]

    # Приоритеты
    priority_catalog_keys = [item["key"] for item in priorities_catalog]
    if priority_preset == "default":
        priorities = [item["key"] for item in priorities_catalog if item.get("is_default")]
    elif priority_preset == "custom":
        selected_priorities = _pick_form_values(priority_values, priority_json)
        priorities = admin._normalize_versions(selected_priorities, priority_catalog_keys)
    else:
        priorities = admin._parse_json_string_list(priority_json) or ["all"]
    if work_hours_from and work_hours_to:
        wh_from = _validate_work_time(work_hours_from, "Время начала")
        wh_to = _validate_work_time(work_hours_to, "Время окончания")
        wh = f"{wh_from}-{wh_to}"
    else:
        wh = work_hours.strip() or None
    if work_days_values:
        wd = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        wd = admin._parse_work_days(work_days_json)
    row = SupportGroup(
        name=n,
        room_id=room,
        timezone=(timezone_name or "").strip() or None,
        is_active=True if is_active is None else is_active in ("1", "on", "true"),
        notify=notify,
        versions=versions,
        priorities=priorities,
        work_hours=wh,
        work_days=wd,
        dnd=dnd in ("1", "on", "true"),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(400, "Не удалось создать группу: проверьте уникальность названия")
    await admin._maybe_log_admin_crud(
        session,
        user,
        "group",
        "create",
        {"id": row.id, "name": n},
    )
    return RedirectResponse(f"/groups?highlight_group_id={row.id}&saved=1", status_code=303)


@router.post("/groups/{group_id}")
async def groups_update(
    request: Request,
    group_id: int,
    name: Annotated[str, Form()],
    room_id: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
    is_active: Annotated[str | None, Form()] = None,
    status_json: Annotated[str, Form()] = "",
    status_preset: Annotated[str, Form()] = "default",
    status_values: Annotated[list[str], Form()] = [],
    version_json: Annotated[str, Form()] = "",
    version_preset: Annotated[str, Form()] = "default",
    version_values: Annotated[list[str], Form()] = [],
    priority_json: Annotated[str, Form()] = "",
    priority_preset: Annotated[str, Form()] = "default",
    priority_values: Annotated[list[str], Form()] = [],
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
    statuses_catalog = await admin._load_statuses_catalog(session)
    versions_catalog = await admin._load_versions_catalog(session)
    priorities_catalog = await admin._load_priorities_catalog(session)
    status_allowed = [item["key"] for item in statuses_catalog]
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
        wh_from = _validate_work_time(work_hours_from, "Время начала")
        wh_to = _validate_work_time(work_hours_to, "Время окончания")
        wh = f"{wh_from}-{wh_to}"
    else:
        wh = work_hours.strip() or None
    if work_days_values:
        wd = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        wd = admin._parse_work_days(work_days_json)

    # Статусы
    if status_preset == "default":
        notify = [item["key"] for item in statuses_catalog if item.get("is_default")]
    elif status_preset == "custom":
        selected_statuses = _pick_form_values(status_values, status_json)
        notify = admin._normalize_notify(selected_statuses, status_allowed)
    else:
        notify = admin._parse_notify(status_json)

    # Версии
    version_catalog_keys = [item["key"] for item in versions_catalog]
    if version_preset == "default":
        versions = [item["key"] for item in versions_catalog if item.get("is_default")]
    elif version_preset == "custom":
        selected_versions = _pick_form_values(version_values, version_json)
        versions = admin._normalize_versions(selected_versions, version_catalog_keys)
    else:
        versions = admin._parse_json_string_list(version_json) or ["all"]

    # Приоритеты
    priority_catalog_keys = [item["key"] for item in priorities_catalog]
    if priority_preset == "default":
        priorities = [item["key"] for item in priorities_catalog if item.get("is_default")]
    elif priority_preset == "custom":
        selected_priorities = _pick_form_values(priority_values, priority_json)
        priorities = admin._normalize_versions(selected_priorities, priority_catalog_keys)
    else:
        priorities = admin._parse_json_string_list(priority_json) or ["all"]
    old_room = (row.room_id or "").strip()
    new_room = (room_id or "").strip()
    row.name = n
    row.room_id = new_room
    row.timezone = (timezone_name or "").strip() or None
    if is_active is not None:
        row.is_active = is_active in ("1", "on", "true")
    row.notify = notify
    row.versions = versions
    row.priorities = priorities
    row.work_hours = wh
    row.work_days = wd
    row.dnd = dnd in ("1", "on", "true")
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
    return RedirectResponse(f"/groups?highlight_group_id={group_id}&saved=1", status_code=303)


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
