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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BotHeartbeat, BotUser, SupportGroup, UserVersionRoute
from database.session import get_session

logger = logging.getLogger("redmine_admin")

router = APIRouter(tags=["users"])

_TIME_RE = __import__("re").compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _validate_work_time(val: str, label: str) -> str:
    val = (val or "").strip()
    if not val:
        raise HTTPException(400, f"{label}: обязательное поле")
    if not _TIME_RE.match(val):
        raise HTTPException(400, f"{label}: неверный формат (ожидается HH:MM, например 09:00)")
    return val


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
    statuses_catalog = await admin._load_statuses_catalog(session)
    versions_catalog = await admin._load_versions_catalog(session)
    priorities_catalog = await admin._load_priorities_catalog(session)
    _nc, _old_versions = await admin._load_catalogs(session)
    matrix_domain = await admin._get_matrix_domain_from_db(session)
    # По умолчанию — только статусы с is_default=True
    status_default_keys = [item["key"] for item in statuses_catalog if item.get("is_default")]
    version_default_keys = [item["key"] for item in versions_catalog if item.get("is_default")]
    priority_default_keys = [item["key"] for item in priorities_catalog if item.get("is_default")]
    return admin.templates.TemplateResponse(
        request,
        "panel/user_form.html",
        {
            "title": "Новый пользователь",
            "u": None,
            "room_localpart": "",
            "matrix_domain": matrix_domain,
            "status_json": json.dumps(status_default_keys, ensure_ascii=False),
            "status_preset": "default",
            "status_selected": status_default_keys,
            "version_json": json.dumps(version_default_keys, ensure_ascii=False),
            "version_preset": "default",
            "version_selected": version_default_keys,
            "priority_json": json.dumps(priority_default_keys, ensure_ascii=False),
            "priority_preset": "default",
            "priority_selected": priority_default_keys,
            "groups": admin._groups_assignable(groups_rows),
            "group_unassigned_display": admin.GROUP_UNASSIGNED_DISPLAY,
            "bot_tz": admin.os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "timezone_top_options": admin._top_timezone_options(),
            "timezone_all_options": admin._standard_timezone_options(),
            "timezone_labels": admin._timezone_labels(admin._standard_timezone_options()),
            "statuses_catalog": statuses_catalog,
            "versions_catalog": versions_catalog,
            "priorities_catalog": priorities_catalog,
        },
    )


@router.post("/users")
async def users_create(
    request: Request,
    redmine_id: Annotated[int, Form()],
    room: Annotated[str, Form()],
    display_name: Annotated[str, Form()] = "",
    group_id: Annotated[str, Form()] = "",
    status_json: Annotated[str, Form()] = "",
    status_preset: Annotated[str, Form()] = "default",
    status_values: Annotated[list[str], Form()] = [],
    version_json: Annotated[str, Form()] = "",
    version_preset: Annotated[str, Form()] = "default",
    version_values: Annotated[list[str], Form()] = [],
    priority_json: Annotated[str, Form()] = "",
    priority_preset: Annotated[str, Form()] = "default",
    priority_values: Annotated[list[str], Form()] = [],
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
    statuses_catalog = await admin._load_statuses_catalog(session)
    versions_catalog = await admin._load_versions_catalog(session)
    priorities_catalog = await admin._load_priorities_catalog(session)
    status_allowed = [item["key"] for item in statuses_catalog]
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
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
        notify = admin._normalize_notify(status_values, status_allowed)
    else:
        notify = admin._parse_notify(status_json)

    # Версии
    version_catalog_keys = [item["key"] for item in versions_catalog]
    if version_preset == "default":
        versions = [item["key"] for item in versions_catalog if item.get("is_default")]
    elif version_preset == "custom":
        versions = admin._normalize_versions(version_values, version_catalog_keys)
    else:
        versions = admin._parse_json_string_list(version_json) or ["all"]

    # Приоритеты
    priority_catalog_keys = [item["key"] for item in priorities_catalog]
    if priority_preset == "default":
        priorities = [item["key"] for item in priorities_catalog if item.get("is_default")]
    elif priority_preset == "custom":
        priorities = admin._normalize_versions(priority_values, priority_catalog_keys)
    else:
        priorities = admin._parse_json_string_list(priority_json) or ["all"]
    full_room = await admin._build_room_id_async(room.strip(), session)
    row = BotUser(
        redmine_id=redmine_id,
        display_name=display_name.strip() or None,
        group_id=int(group_id) if str(group_id).isdigit() else None,
        department=None,
        room=full_room,
        notify=notify,
        versions=versions,
        priorities=priorities,
        timezone=(timezone_name or "").strip() or None,
        work_hours=wh,
        work_days=wd,
        dnd=dnd in ("on", "true", "1"),
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(
            400, "Не удалось создать пользователя: проверьте уникальность redmine_id"
        )
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
    return RedirectResponse(f"/users?highlight_user_id={row.id}&saved=1", status_code=303)


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

    logger = admin.logger

    # ── Диагностика: Matrix client ──
    client = await admin._get_matrix_client(session)
    if not client:
        logger.error("[DIAG] Test message: Matrix client is None")
        return JSONResponse(
            {"ok": False, "error": "Matrix не настроен (нет homeserver/token/user_id)"},
            status_code=400,
        )

    redmine_url = await admin._load_secret_plain(session, "REDMINE_URL")
    redmine_key = await admin._load_secret_plain(session, "REDMINE_API_KEY")
    bot_mxid = await admin._load_secret_plain(session, "MATRIX_USER_ID")

    logger.info(
        "[DIAG] Test message: bot_mxid='%s', redmine_url='%s', redmine_key_len=%d",
        bot_mxid,
        redmine_url,
        len(redmine_key) if redmine_key else 0,
    )

    form = await request.form()
    raw_uid = form.get("user_id", "")
    raw_mxid = form.get("mxid", "")

    logger.info("[DIAG] Test message: raw_uid='%s', raw_mxid='%s'", raw_uid, raw_mxid)

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
    logger.info(
        "[DIAG] Test message: homeserver='%s', matrix_domain='%s'", homeserver, matrix_domain
    )

    if target_mxid and ":" not in target_mxid:
        if not target_mxid.startswith("@"):
            target_mxid = f"@{target_mxid}"
        target_mxid = f"{target_mxid}:{matrix_domain}"

    if uid > 0:
        row = await session.get(BotUser, uid)
        if not row:
            await client.close()
            return JSONResponse({"ok": False, "error": "Пользователь не найден"}, status_code=404)

        logger.info(
            "[DIAG] Test message: user row: id=%d, room='%s', redmine_id=%d, group_id=%s",
            row.id,
            row.room,
            row.redmine_id,
            row.group_id,
        )

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

        logger.info(
            "[DIAG] Test message: after room processing: target_mxid='%s', room_id='%s'",
            target_mxid,
            room_id,
        )

        if not target_mxid and not room_id and redmine_url and redmine_key and row.redmine_id:
            try:
                from redmine_cache import fetch_redmine_user_by_id

                logger.info("[DIAG] Test message: fetching Redmine user id=%d", row.redmine_id)
                rdata, err = fetch_redmine_user_by_id(row.redmine_id, redmine_url, redmine_key)
                logger.info(
                    "[DIAG] Test message: Redmine fetch result: err=%s, rdata keys=%s",
                    err,
                    list(rdata.keys()) if rdata else None,
                )
                if rdata:
                    login = rdata.get("login", "")
                    if login:
                        domain = bot_mxid.split(":", 1)[1] if ":" in bot_mxid else ""
                        target_mxid = f"@{login}:{domain}" if domain else None
                        logger.info(
                            "[DIAG] Test message: resolved target_mxid from Redmine login='%s' → '%s'",
                            login,
                            target_mxid,
                        )
            except Exception as e:
                logger.error("[DIAG] Test message: Redmine fetch exception: %s", e, exc_info=True)
                pass

    logger.info("[DIAG] Test message: FINAL target_mxid='%s', room_id='%s'", target_mxid, room_id)

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


# ═══════════════════════════════════════════════════════════════════════════
# bulk-delete ДОЛЖЕН идти ПЕРЕД всеми /users/{user_id}..., иначе FastAPI
# интерпретирует "bulk-delete" как user_id=int → 422 ошибка
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/users/bulk-delete")
async def users_bulk_delete(
    request: Request,
    user_ids: Annotated[list[str], Form()],
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    """Массовое удаление пользователей."""
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        return JSONResponse({"success": False, "error": "Только admin"}, status_code=403)

    deleted_count = 0
    for uid in user_ids:
        if uid.isdigit():
            row = await session.get(BotUser, int(uid))
            if row:
                await session.delete(row)
                deleted_count += 1

    await session.commit()
    await admin._maybe_log_admin_crud(
        session, user, "bot_user", "bulk_delete", {"count": deleted_count, "ids": user_ids}
    )
    return JSONResponse({"success": True, "deleted": deleted_count})


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
    statuses_catalog = await admin._load_statuses_catalog(session)
    versions_catalog = await admin._load_versions_catalog(session)
    priorities_catalog = await admin._load_priorities_catalog(session)
    matrix_domain = await admin._get_matrix_domain_from_db(session)

    status_keys = {item["key"] for item in statuses_catalog}
    status_default_keys = [item["key"] for item in statuses_catalog if item.get("is_default")]
    notify_selected = [str(x).strip() for x in (row.notify or ["all"]) if str(x).strip()]
    preset = admin._status_preset(row.notify)
    if preset == "default":
        status_selected = status_default_keys
    else:
        status_selected = [k for k in notify_selected if k in status_keys]

    # Версии (из БД, пока legacy)
    version_default_keys = [item["key"] for item in versions_catalog if item.get("is_default")]
    version_selected = row.versions or []
    version_preset = "default" if (not version_selected or version_selected == ["all"]) else "custom"
    if version_preset == "default":
        version_selected = version_default_keys

    # Приоритеты
    priority_default_keys = [item["key"] for item in priorities_catalog if item.get("is_default")]
    priority_selected = row.priorities or []
    priority_preset = "default" if (not priority_selected or priority_selected == ["all"]) else "custom"
    if priority_preset == "default":
        priority_selected = priority_default_keys

    return admin.templates.TemplateResponse(
        request,
        "panel/user_form.html",
        {
            "title": f"Пользователь Redmine {row.redmine_id}",
            "u": row,
            "room_localpart": admin._room_localpart(row.room),
            "matrix_domain": matrix_domain,
            # Statuses
            "status_json": json.dumps(row.notify, ensure_ascii=False),
            "status_preset": preset,
            "status_selected": status_selected,
            # Versions
            "version_json": json.dumps(row.versions, ensure_ascii=False),
            "version_preset": version_preset,
            "version_selected": version_selected,
            # Priorities
            "priority_json": json.dumps(row.priorities, ensure_ascii=False),
            "priority_preset": priority_preset,
            "priority_selected": priority_selected,
            # Catalogs
            "statuses_catalog": statuses_catalog,
            "versions_catalog": versions_catalog,
            "priorities_catalog": priorities_catalog,
            "groups": admin._groups_assignable(groups_rows),
            "group_unassigned_display": admin.GROUP_UNASSIGNED_DISPLAY,
            "bot_tz": admin.os.getenv("BOT_TIMEZONE", "Europe/Moscow"),
            "timezone_top_options": admin._top_timezone_options(),
            "timezone_all_options": admin._standard_timezone_options(),
            "timezone_labels": admin._timezone_labels(admin._standard_timezone_options()),
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
    status_json: Annotated[str, Form()] = "",
    status_preset: Annotated[str, Form()] = "all",
    status_values: Annotated[list[str], Form()] = [],
    version_preset: Annotated[str, Form()] = "default",
    version_values: Annotated[list[str], Form()] = [],
    version_json: Annotated[str, Form()] = "",
    priority_preset: Annotated[str, Form()] = "default",
    priority_values: Annotated[list[str], Form()] = [],
    priority_json: Annotated[str, Form()] = "",
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
    statuses_catalog = await admin._load_statuses_catalog(session)
    versions_catalog = await admin._load_versions_catalog(session)
    priorities_catalog = await admin._load_priorities_catalog(session)
    status_allowed = [item["key"] for item in statuses_catalog]
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
    # Статусы
    if status_preset == "default":
        row.notify = [item["key"] for item in statuses_catalog if item.get("is_default")]
    elif status_preset == "custom":
        row.notify = admin._normalize_notify(status_values, status_allowed)
    else:
        row.notify = admin._parse_notify(status_json)

    if work_hours_from and work_hours_to:
        wh_from = _validate_work_time(work_hours_from, "Время начала")
        wh_to = _validate_work_time(work_hours_to, "Время окончания")
        row.work_hours = f"{wh_from}-{wh_to}"
    else:
        row.work_hours = work_hours.strip() or None
    if work_days_values:
        row.work_days = sorted({int(v) for v in work_days_values if str(v).isdigit()})
    else:
        row.work_days = admin._parse_work_days(work_days_json)
    row.dnd = dnd in ("on", "true", "1")

    # Версии
    version_catalog_keys = [item["key"] for item in versions_catalog]
    if version_preset == "default":
        row.versions = [item["key"] for item in versions_catalog if item.get("is_default")]
    elif version_preset == "custom":
        row.versions = admin._normalize_versions(version_values, version_catalog_keys)
    else:
        row.versions = admin._parse_json_string_list(version_json) or ["all"]

    # Приоритеты
    priority_catalog_keys = [item["key"] for item in priorities_catalog]
    if priority_preset == "default":
        row.priorities = [item["key"] for item in priorities_catalog if item.get("is_default")]
    elif priority_preset == "custom":
        row.priorities = admin._normalize_versions(priority_values, priority_catalog_keys)
    else:
        row.priorities = admin._parse_json_string_list(priority_json) or ["all"]

    await admin._maybe_log_admin_crud(
        session,
        user,
        "bot_user",
        "update",
        {"id": user_id, "redmine_id": redmine_id},
    )
    return RedirectResponse(f"/users?highlight_user_id={user_id}&saved=1", status_code=303)


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
