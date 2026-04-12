"""Users routes: /users."""

from __future__ import annotations

import json
import logging
from datetime import UTC
from datetime import datetime as _dt
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BotHeartbeat, BotUser, SupportGroup, UserVersionRoute
from database.session import get_session

logger = logging.getLogger("redmine_admin")

router = APIRouter(tags=["users"])


def _admin() -> object:
    """Late import to avoid circular dependency with main.py."""
    import admin.main as _m

    return _m


# --- User CRUD ---


@router.get("/users", response_class=HTMLResponse)
async def users_list(
    request: Request,
    q: str = "",
    group_id: str = "",
    highlight_user_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    # Пустая строка → None (Все группы), иначе int
    grp: int | None = int(group_id) if group_id.strip() else None

    groups_rows = list(
        (await session.execute(select(SupportGroup).order_by(SupportGroup.name.asc())))
        .scalars()
        .all()
    )
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
    if grp is not None:
        if grp == -1:
            stmt = stmt.where(BotUser.group_id.is_(None))
        else:
            stmt = stmt.where(BotUser.group_id == grp)
    stmt = stmt.order_by(
        BotUser.group_id.asc().nulls_last(),
        BotUser.display_name.asc().nulls_last(),
        BotUser.redmine_id,
    )
    rows = list((await session.execute(stmt)).scalars().all())

    grouped: dict[str, list[BotUser]] = {}
    for row in rows:
        key = admin._group_display_name(groups_by_id, row.group_id)
        grouped.setdefault(key, []).append(row)

    return admin.templates.TemplateResponse(
        request,
        "panel/users_list.html",
        {
            "users": rows,
            "grouped_users": grouped,
            "groups": admin._groups_assignable(groups_rows),
            "groups_by_id": groups_by_id,
            "q": q,
            "group_filter": group_id,
            "highlight_user_id": highlight_user_id,
            "list_total": len(rows),
        },
    )


@router.get("/users/new", response_class=HTMLResponse)
async def users_new(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    groups_rows = list(
        (await session.execute(select(SupportGroup).order_by(SupportGroup.name.asc())))
        .scalars()
        .all()
    )
    notify_catalog, versions_catalog = await admin._load_catalogs(session)
    matrix_domain = await admin._get_matrix_domain_from_db(session)
    return admin.templates.TemplateResponse(
        request,
        "panel/user_form.html",
        {
            "title": "Новый пользователь",
            "u": None,
            "room_localpart": "",
            "matrix_domain": matrix_domain,
            "notify_json": '["all"]',
            "notify_preset": "all",
            "notify_selected": ["all"],
            "groups": admin._groups_assignable(groups_rows),
            "group_unassigned_display": admin.GROUP_UNASSIGNED_DISPLAY,
            "bot_tz": admin.os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "timezone_top_options": admin._top_timezone_options(),
            "timezone_all_options": admin._standard_timezone_options(),
            "timezone_labels": admin._timezone_labels(admin._standard_timezone_options()),
            "notify_catalog": notify_catalog,
            "versions_catalog": versions_catalog,
            "selected_version_keys": [],
            "version_preset": "all",
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
    initial_version_keys: Annotated[str, Form()] = "",
    version_keys_json: Annotated[str, Form()] = "",
    version_preset: Annotated[str, Form()] = "all",
    version_values: Annotated[list[str], Form()] = [],
    timezone_name: Annotated[str, Form()] = "",
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
    full_room = await admin._build_room_id_async(room.strip(), session)
    row = BotUser(
        redmine_id=redmine_id,
        display_name=display_name.strip() or None,
        group_id=int(group_id) if str(group_id).isdigit() else None,
        department=None,
        room=full_room,
        notify=notify,
        timezone=(timezone_name or "").strip() or None,
        work_hours=wh,
        work_days=wd,
        dnd=dnd in ("on", "true", "1"),
    )
    session.add(row)
    await session.flush()
    if version_preset == "all":
        version_keys = list(versions_catalog)
    elif version_preset == "custom":
        version_keys = admin._normalize_versions(version_values, versions_catalog)
    else:
        version_keys = admin._parse_json_string_list(
            version_keys_json
        ) or admin._parse_status_keys_list(initial_version_keys)
    for vkey in version_keys:
        ex = await session.execute(
            select(UserVersionRoute.id).where(
                UserVersionRoute.bot_user_id == row.id,
                UserVersionRoute.version_key == vkey,
            )
        )
        if ex.scalar_one_or_none():
            continue
        session.add(UserVersionRoute(bot_user_id=row.id, version_key=vkey, room_id=row.room))
    await admin._maybe_log_admin_crud(
        session,
        user,
        "bot_user",
        "create",
        {
            "id": row.id,
            "redmine_id": redmine_id,
            "group_id": row.group_id,
        },
    )
    return RedirectResponse(f"/users?highlight_user_id={row.id}", status_code=303)


@router.post("/users/test-message")
async def user_test_message(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Отправляет тестовое сообщение по user_id (из БД) или напрямую по MXID."""
    admin = _admin()
    admin._verify_csrf_json(request)
    admin_user = getattr(request.state, "current_user", None)
    if not admin_user or getattr(admin_user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    client = await admin._get_matrix_client(session)
    if not client:
        return JSONResponse(
            {"ok": False, "error": "Matrix не настроен (нет homeserver/token/user_id)"},
            status_code=400,
        )

    redmine_url = await admin._load_secret_plain(session, "REDMINE_URL")
    redmine_key = await admin._load_secret_plain(session, "REDMINE_API_KEY")
    bot_mxid = await admin._load_secret_plain(session, "MATRIX_USER_ID")

    form = await request.form()
    raw_uid = form.get("user_id", "")
    raw_mxid = form.get("mxid", "")

    uid = 0
    if raw_uid:
        try:
            uid = int(raw_uid)
        except ValueError:
            uid = 0

    target_mxid = (raw_mxid or "").strip()
    room_id = None

    homeserver = client.homeserver
    matrix_domain = homeserver.replace("https://", "").replace("http://", "").rstrip("/")

    if target_mxid and ":" not in target_mxid:
        if not target_mxid.startswith("@"):
            target_mxid = f"@{target_mxid}"
        target_mxid = f"{target_mxid}:{matrix_domain}"

    if uid > 0:
        row = await session.get(BotUser, uid)
        if not row:
            await client.close()
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=404)

        raw_room = (row.room or "").strip()

        if raw_room.startswith("@"):
            if ":" not in raw_room and matrix_domain:
                target_mxid = f"{raw_room}:{matrix_domain}"
            else:
                target_mxid = raw_room
            room_id = None
        elif raw_room.startswith("!"):
            room_id = raw_room
        elif raw_room:
            if matrix_domain:
                target_mxid = f"@{raw_room}:{matrix_domain}"
            else:
                target_mxid = f"@{raw_room}"
            room_id = None

        if not target_mxid and not room_id and redmine_url and redmine_key and row.redmine_id:
            try:
                import json as _json3
                from urllib.request import Request, urlopen

                api_url = f"{redmine_url.rstrip('/')}/users/{row.redmine_id}.json"
                req = Request(
                    api_url,
                    headers={"X-Redmine-API-Key": redmine_key, "Accept": "application/json"},
                )
                with urlopen(req, timeout=10) as resp:
                    rdata = _json3.loads(resp.read().decode())
                    login = rdata.get("user", {}).get("login", "")
                    if login:
                        domain = bot_mxid.split(":", 1)[1] if ":" in bot_mxid else ""
                        target_mxid = f"@{login}:{domain}" if domain else None
            except Exception:
                pass

    if not target_mxid and not room_id:
        await client.close()
        return JSONResponse(
            {"ok": False, "error": "Не указан Matrix ID пользователя"}, status_code=400
        )

    from src.matrix_send import room_send_with_retry

    ts = _dt.now().strftime("%H:%M:%S")
    html = (
        f"<b>Тестовое сообщение</b><br>"
        f"Это тест от панели управления.<br>"
        f"Если вы это видите — подключение работает!<br>"
        f"<small>Отправлено: {ts}</small>"
    )
    text_plain = f"Тестовое сообщение\nЭто тест от панели управления.\nОтправлено: {ts}"

    final_room_id = room_id

    try:
        if not final_room_id and target_mxid:
            logger.info("test_message: syncing to find DM for %s", target_mxid)
            await admin._sync_matrix_client(client)

            for r_id, room_obj in client.rooms.items():
                member_ids = {m.user_id for m in room_obj.users.values()}
                if len(member_ids) == 2 and bot_mxid in member_ids and target_mxid in member_ids:
                    final_room_id = r_id
                    logger.info("test_message: found existing DM %s", r_id)
                    break
            if not final_room_id:
                logger.info("test_message: creating DM with %s", target_mxid)
                resp_create = await client.room_create(
                    invite=[target_mxid],
                    is_direct=True,
                )
                if resp_create and hasattr(resp_create, "room_id"):
                    final_room_id = resp_create.room_id
                    logger.info("test_message: created DM %s, joining...", final_room_id)
                    await client.join(final_room_id)
                else:
                    err_detail = str(resp_create) if resp_create else "no response"
                    await client.close()
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": f"Не удалось создать DM с {target_mxid}: {err_detail}",
                        },
                        status_code=500,
                    )

        if not final_room_id:
            await client.close()
            return JSONResponse(
                {"ok": False, "error": "Не удалось определить комнату"}, status_code=500
            )

        logger.info("test_message: sending to %s", final_room_id)
        content = {
            "msgtype": "m.text",
            "body": text_plain,
            "format": "org.matrix.custom.html",
            "formatted_body": html,
        }
        await room_send_with_retry(client, final_room_id, content)
        await client.close()
        return JSONResponse({"ok": True})
    except Exception as e:
        import traceback as _tb

        logger.error(
            "test_message_failed uid=%s mxid=%s error=%s\n%s", uid, target_mxid, e, _tb.format_exc()
        )
        await client.close()
        return JSONResponse(
            {"ok": False, "error": "Не удалось отправить сообщение. Проверьте логи админки."},
            status_code=500,
        )


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def users_edit(
    request: Request,
    user_id: int,
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(BotUser, user_id)
    if not row:
        raise HTTPException(404)
    version_err = (request.query_params.get("version_err") or "").strip()
    version_msg = (request.query_params.get("version_msg") or "").strip()
    uv_stmt = (
        select(UserVersionRoute)
        .where(UserVersionRoute.bot_user_id == user_id)
        .order_by(UserVersionRoute.version_key)
    )
    version_rows = list((await session.execute(uv_stmt)).scalars().all())
    groups_rows = list(
        (await session.execute(select(SupportGroup).order_by(SupportGroup.name.asc())))
        .scalars()
        .all()
    )
    notify_catalog, versions_catalog = await admin._load_catalogs(session)
    matrix_domain = await admin._get_matrix_domain_from_db(session)
    notify_keys = {item["key"] for item in notify_catalog}
    notify_selected = [str(x).strip() for x in (row.notify or ["all"]) if str(x).strip()]
    if "all" not in notify_selected:
        notify_selected = [k for k in notify_selected if k in notify_keys]
    version_set = set(versions_catalog)
    selected_versions = [r.version_key for r in version_rows if r.version_key in version_set]
    return admin.templates.TemplateResponse(
        request,
        "panel/user_form.html",
        {
            "title": f"Пользователь Redmine {row.redmine_id}",
            "u": row,
            "room_localpart": admin._room_localpart(row.room),
            "matrix_domain": matrix_domain,
            "notify_json": json.dumps(row.notify, ensure_ascii=False),
            "notify_preset": admin._notify_preset(row.notify),
            "notify_selected": notify_selected,
            "groups": admin._groups_assignable(groups_rows),
            "group_unassigned_display": admin.GROUP_UNASSIGNED_DISPLAY,
            "bot_tz": admin.os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "timezone_top_options": admin._top_timezone_options(),
            "timezone_all_options": admin._standard_timezone_options(),
            "timezone_labels": admin._timezone_labels(admin._standard_timezone_options()),
            "version_routes": version_rows,
            "version_keys_text": "\n".join(r.version_key for r in version_rows),
            "version_err": version_err,
            "version_msg": version_msg,
            "notify_catalog": notify_catalog,
            "versions_catalog": versions_catalog,
            "selected_version_keys": selected_versions,
            "version_preset": admin._version_preset(selected_versions, versions_catalog),
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
    version_preset: Annotated[str, Form()] = "all",
    version_values: Annotated[list[str], Form()] = [],
    version_keys_text: Annotated[str, Form()] = "",
    version_keys_json: Annotated[str, Form()] = "",
    timezone_name: Annotated[str, Form()] = "",
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
    row = await session.get(BotUser, user_id)
    if not row:
        raise HTTPException(404)
    old_room = (row.room or "").strip()
    new_room = await admin._build_room_id_async(room.strip(), session)
    row.redmine_id = redmine_id
    row.display_name = display_name.strip() or None
    row.group_id = int(group_id) if str(group_id).isdigit() else None
    row.room = new_room
    row.timezone = (timezone_name or "").strip() or None
    if notify_preset == "all":
        row.notify = ["all"]
    elif notify_preset == "new_only":
        row.notify = ["new"]
    elif notify_preset == "overdue_only":
        row.notify = ["overdue"]
    elif notify_preset == "custom":
        row.notify = admin._normalize_notify(notify_values, notify_allowed)
    else:
        row.notify = admin._parse_notify(notify_json)
    if work_hours_from and work_hours_to:
        row.work_hours = f"{work_hours_from.strip()}-{work_hours_to.strip()}"
    else:
        row.work_hours = work_hours.strip() or None
    if work_days_values:
        row.work_days = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        row.work_days = admin._parse_work_days(work_days_json)
    row.dnd = dnd in ("on", "true", "1")
    if version_preset == "all":
        submitted_versions = list(versions_catalog)
    elif version_preset == "custom":
        submitted_versions = admin._normalize_versions(version_values, versions_catalog)
    else:
        submitted_versions = admin._parse_json_string_list(
            version_keys_json
        ) or admin._parse_status_keys_list(version_keys_text)
    existing_routes = list(
        (
            await session.execute(
                select(UserVersionRoute).where(UserVersionRoute.bot_user_id == user_id)
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
        session.add(UserVersionRoute(bot_user_id=user_id, version_key=key, room_id=new_room))
    if old_room and new_room and old_room != new_room:
        await session.execute(
            update(UserVersionRoute)
            .where(UserVersionRoute.bot_user_id == user_id, UserVersionRoute.room_id == old_room)
            .values(room_id=new_room)
        )
    await admin._maybe_log_admin_crud(
        session,
        user,
        "bot_user",
        "update",
        {"id": user_id, "redmine_id": redmine_id},
    )
    return RedirectResponse(f"/users?highlight_user_id={user_id}", status_code=303)


@router.post("/users/{user_id}/version-routes/add")
async def user_version_route_add(
    request: Request,
    user_id: int,
    version_key: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(BotUser, user_id)
    if not row:
        raise HTTPException(404)
    room = (row.room or "").strip()
    if not room:
        return RedirectResponse(f"/users/{user_id}/edit?version_err=no_room", status_code=303)
    key = (version_key or "").strip()
    if not key:
        return RedirectResponse(f"/users/{user_id}/edit?version_err=empty", status_code=303)
    exists = await session.execute(
        select(UserVersionRoute.id).where(
            UserVersionRoute.bot_user_id == user_id,
            UserVersionRoute.version_key == key,
        )
    )
    if exists.scalar_one_or_none():
        return RedirectResponse(f"/users/{user_id}/edit?version_err=exists", status_code=303)
    session.add(UserVersionRoute(bot_user_id=user_id, version_key=key, room_id=room))
    await admin._maybe_log_admin_crud(
        session,
        user,
        "user_version_route",
        "create",
        {"bot_user_id": user_id, "version_key": key},
    )
    return RedirectResponse(f"/users/{user_id}/edit?version_msg=added", status_code=303)


@router.post("/users/{user_id}/version-routes/{route_row_id}/delete")
async def user_version_route_delete(
    request: Request,
    user_id: int,
    route_row_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    rte = await session.get(UserVersionRoute, route_row_id)
    if not rte or rte.bot_user_id != user_id:
        raise HTTPException(404, "Маршрут не найден")
    vkey = rte.version_key
    await session.delete(rte)
    await admin._maybe_log_admin_crud(
        session,
        user,
        "user_version_route",
        "delete",
        {"bot_user_id": user_id, "version_key": vkey, "route_id": route_row_id},
    )
    return RedirectResponse(f"/users/{user_id}/edit?version_msg=deleted", status_code=303)


@router.post("/users/{user_id}/delete")
async def users_delete(
    request: Request,
    user_id: int,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    row = await session.get(BotUser, user_id)
    if row:
        uid, rmid = row.id, row.redmine_id
        await session.delete(row)
        await admin._maybe_log_admin_crud(
            session, user, "bot_user", "delete", {"id": uid, "redmine_id": rmid}
        )
    return RedirectResponse("/users", status_code=303)


# --- Bot Heartbeat API ---


@router.post("/api/bot/heartbeat")
async def bot_heartbeat_post(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Бот вызывает этот endpoint раз в минуту, чтобы сообщить, что он жив."""
    try:
        data = await request.json()
        instance_id_str = data.get("instance_id")
        if not instance_id_str:
            return JSONResponse({"ok": False, "error": "instance_id required"}, status_code=400)

        import uuid

        instance_id = uuid.UUID(instance_id_str)

        stmt = select(BotHeartbeat).where(BotHeartbeat.instance_id == instance_id)
        result = await session.execute(stmt)
        hb = result.scalar_one_or_none()

        if hb:
            from datetime import datetime

            hb.last_seen = datetime.now(UTC)
        else:
            from datetime import datetime

            session.add(BotHeartbeat(instance_id=instance_id, last_seen=datetime.now(UTC)))

        await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error("heartbeat_post_failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/api/bot/status", response_class=JSONResponse)
async def bot_status_get(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Возвращает статус бота для дашборда."""
    try:
        from datetime import datetime

        stmt = select(BotHeartbeat).order_by(BotHeartbeat.last_seen.desc()).limit(1)
        result = await session.execute(stmt)
        hb = result.scalar_one_or_none()

        if not hb:
            return {
                "status": "unknown",
                "last_seen": None,
                "message": "Бот ещё не отправлял heartbeat",
            }

        now = datetime.now(UTC)
        diff = (now - hb.last_seen).total_seconds()

        if diff < 120:
            status = "alive"
            message = f"Бот активен ({int(diff)} сек. назад)"
        elif diff < 600:
            status = "warning"
            message = f"Бот может быть завис ({int(diff)} сек. назад)"
        else:
            status = "dead"
            message = f"Бот не отвечает ({int(diff)} сек. назад)"

        return {
            "status": status,
            "last_seen": hb.last_seen.isoformat(),
            "message": message,
            "seconds_ago": int(diff),
        }
    except Exception as e:
        logger.error("bot_status_failed: %s", e)
        return {"status": "error", "message": str(e)}
