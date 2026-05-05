"""
Загрузка USERS / STATUS_ROOM_MAP / VERSION_ROOM_MAP из Postgres
в формате, совместимом с bot.py и .env JSON.

См. двойные проекции маршрутов (мапы vs routes_config): docs/RUNTIME_ROUTING_CONFIG.md
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    BotUser,
    CycleSettings,
    GroupVersionRoute,
    StatusRoomRoute,
    SupportGroup,
    UserVersionRoute,
    VersionRoomRoute,
)
from .session import get_session_factory

logger = logging.getLogger("redmine_bot")


def _routes_and_flat_map(
    rows: list[Any],
    *,
    key_field: str,
    source_name: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """
    Строит одновременно плоскую map-проекцию и list-проекцию маршрутов.

    Это единая точка сборки для глобальных status/version route-таблиц.
    """
    flat_map: dict[str, str] = {}
    routes: list[dict[str, Any]] = []
    for row in rows:
        key = str(getattr(row, key_field))
        room_id = str(getattr(row, "room_id"))
        flat_map[key] = room_id
        routes.append(
            {
                key_field: key,
                "room_id": room_id,
                "priority": int(getattr(row, "priority")),
                "sort_order": int(getattr(row, "sort_order")),
                "notify_on_assignment": bool(getattr(row, "notify_on_assignment")),
                "route_source": source_name,
                "route_id": int(getattr(row, "id")),
            }
        )
    return flat_map, routes


def user_orm_to_cfg(
    row: BotUser,
    groups_by_id: dict[int, SupportGroup],
    gv_by_group: dict[int, list[dict[str, str]]] | None = None,
    uv_by_user: dict[int, list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    gv_by_group = gv_by_group or {}
    uv_by_user = uv_by_user or {}
    d: dict[str, Any] = {
        "id": row.id,
        "redmine_id": row.redmine_id,
        "room": row.room,
        "notify": row.notify if isinstance(row.notify, list) else ["all"],
        "versions": row.versions if isinstance(row.versions, list) else ["all"],
        "priorities": row.priorities if isinstance(row.priorities, list) else ["all"],
    }
    if row.group_id is not None:
        d["group_id"] = row.group_id
        g = groups_by_id.get(row.group_id)
        if g is not None:
            d["group_name"] = g.name
            d["group_room"] = g.room_id
            d["group_notify_on_assignment"] = bool(getattr(g, "notify_on_assignment", True))
            if g.timezone:
                d["group_timezone"] = g.timezone
            d["group_delivery"] = {
                "notify": g.notify if isinstance(g.notify, list) else ["all"],
                "versions": g.versions if isinstance(g.versions, list) else ["all"],
                "priorities": g.priorities if isinstance(g.priorities, list) else ["all"],
                "work_hours": g.work_hours,
                "work_days": g.work_days,
                "dnd": bool(g.dnd),
            }
    if row.timezone:
        d["timezone"] = row.timezone
    if row.work_hours:
        d["work_hours"] = row.work_hours
    if row.work_days is not None:
        d["work_days"] = row.work_days
    if row.dnd:
        d["dnd"] = True
    ciph = getattr(row, "redmine_api_key_ciphertext", None)
    nonce = getattr(row, "redmine_api_key_nonce", None)
    if ciph and nonce:
        # Только для выбора Redmine-клиента в bot.py; не логировать эти ключи.
        d["_redmine_key_cipher"] = ciph
        d["_redmine_key_nonce"] = nonce
    vr: list[dict[str, str]] = []
    vr.extend(uv_by_user.get(row.id, []))
    if row.group_id is not None:
        vr.extend(gv_by_group.get(row.group_id, []))
    d["version_routes"] = vr
    return d


def group_orm_to_cfg(row: SupportGroup) -> dict[str, Any]:
    out: dict[str, Any] = {
        "group_id": row.id,
        "group_name": row.name,
        "room": row.room_id,
        "notify_on_assignment": bool(getattr(row, "notify_on_assignment", True)),
        "notify": row.notify if isinstance(row.notify, list) else ["all"],
        "versions": row.versions if isinstance(row.versions, list) else ["all"],
        "priorities": row.priorities if isinstance(row.priorities, list) else ["all"],
        "work_hours": row.work_hours,
        "work_days": row.work_days,
        "dnd": bool(row.dnd),
    }
    if row.timezone:
        out["timezone"] = row.timezone
    return out


async def fetch_runtime_config(
    session: AsyncSession | None = None,
) -> tuple[list, dict, dict, list, dict[str, Any]]:
    """
    Возвращает (USERS, STATUS_ROOM_MAP, VERSION_ROOM_MAP, GROUPS, routes_config).

    ``routes_config`` — метаданные маршрутов для ``bot.routing``:
    ``status_routes``, ``version_routes_global`` (списки словарей).
    """
    if session is None:
        factory = get_session_factory()
        async with factory() as s:
            return await fetch_runtime_config(s)

    r_groups = await session.execute(select(SupportGroup))
    groups = list(r_groups.scalars().all())
    groups_by_id = {g.id: g for g in groups}

    gv_by_group: dict[int, list[dict[str, Any]]] = defaultdict(list)
    r_gv = await session.execute(
        select(GroupVersionRoute).order_by(
            GroupVersionRoute.priority,
            GroupVersionRoute.sort_order,
            GroupVersionRoute.id,
        )
    )
    for gr in r_gv.scalars().all():
        gv_by_group[gr.group_id].append(
            {
                "key": gr.version_key,
                "room": gr.room_id,
                "priority": int(gr.priority),
                "sort_order": int(gr.sort_order),
                "notify_on_assignment": bool(gr.notify_on_assignment),
                "route_source": "group_version_route",
                "route_id": gr.id,
            }
        )

    uv_by_user: dict[int, list[dict[str, Any]]] = defaultdict(list)
    r_uv = await session.execute(
        select(UserVersionRoute).order_by(
            UserVersionRoute.priority,
            UserVersionRoute.sort_order,
            UserVersionRoute.id,
        )
    )
    for ur in r_uv.scalars().all():
        uv_by_user[ur.bot_user_id].append(
            {
                "key": ur.version_key,
                "room": ur.room_id,
                "priority": int(ur.priority),
                "sort_order": int(ur.sort_order),
                "notify_on_assignment": bool(ur.notify_on_assignment),
                "route_source": "user_version_route",
                "route_id": ur.id,
            }
        )

    r_users = await session.execute(select(BotUser).order_by(BotUser.redmine_id))
    users = [
        user_orm_to_cfg(u, groups_by_id, gv_by_group, uv_by_user) for u in r_users.scalars().all()
    ]
    groups_cfg = [group_orm_to_cfg(g) for g in groups]

    r_st = await session.execute(
        select(StatusRoomRoute).order_by(
            StatusRoomRoute.priority,
            StatusRoomRoute.sort_order,
            StatusRoomRoute.id,
        )
    )
    status_rows = list(r_st.scalars().all())
    status_map, status_routes = _routes_and_flat_map(
        status_rows,
        key_field="status_key",
        source_name="status_room_route",
    )

    r_ver = await session.execute(
        select(VersionRoomRoute).order_by(
            VersionRoomRoute.priority,
            VersionRoomRoute.sort_order,
            VersionRoomRoute.id,
        )
    )
    version_rows = list(r_ver.scalars().all())
    version_map, version_routes_global = _routes_and_flat_map(
        version_rows,
        key_field="version_key",
        source_name="version_room_route",
    )

    routes_config: dict[str, Any] = {
        "status_routes": status_routes,
        "version_routes_global": version_routes_global,
    }

    return users, status_map, version_map, groups_cfg, routes_config


async def row_counts(session: AsyncSession | None = None) -> tuple[int, int, int]:
    if session is None:
        factory = get_session_factory()
        async with factory() as s:
            return await row_counts(s)
    nu = await session.scalar(select(func.count()).select_from(BotUser))
    ns = await session.scalar(select(func.count()).select_from(StatusRoomRoute))
    nv = await session.scalar(select(func.count()).select_from(VersionRoomRoute))
    return int(nu or 0), int(ns or 0), int(nv or 0)


# src/database/load_config.py — добавить в конец файла:


async def fetch_cycle_settings(session: AsyncSession | None = None) -> dict[str, str]:
    """
    Загружает настройки цикла из таблицы cycle_settings.
    Возвращает {key: value} — например {"CHECK_INTERVAL": "90", "REMINDER_AFTER": "3600"}.
    """
    if session is None:
        factory = get_session_factory()
        async with factory() as s:
            return await fetch_cycle_settings(s)

    result = await session.execute(select(CycleSettings))
    return {row.key: row.value for row in result.scalars().all()}
