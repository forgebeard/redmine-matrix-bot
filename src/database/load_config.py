"""
Загрузка USERS / STATUS_ROOM_MAP / VERSION_ROOM_MAP из Postgres
в формате, совместимом с bot.py и .env JSON.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import BotUser, StatusRoomRoute, SupportGroup, VersionRoomRoute
from .session import get_session_factory

logger = logging.getLogger("redmine_bot")


def user_orm_to_cfg(row: BotUser, groups_by_id: dict[int, SupportGroup]) -> dict[str, Any]:
    d: dict[str, Any] = {
        "redmine_id": row.redmine_id,
        "room": row.room,
        "notify": row.notify if isinstance(row.notify, list) else ["all"],
    }
    if row.group_id is not None:
        d["group_id"] = row.group_id
        g = groups_by_id.get(row.group_id)
        if g is not None:
            d["group_name"] = g.name
            d["group_room"] = g.room_id
            if g.timezone:
                d["group_timezone"] = g.timezone
    if row.work_hours:
        d["work_hours"] = row.work_hours
    if row.work_days is not None:
        d["work_days"] = row.work_days
    if row.dnd:
        d["dnd"] = True
    return d


async def fetch_runtime_config(session: AsyncSession | None = None) -> tuple[list, dict, dict]:
    """
    Возвращает (USERS, STATUS_ROOM_MAP, VERSION_ROOM_MAP).
    """
    if session is None:
        factory = get_session_factory()
        async with factory() as s:
            return await fetch_runtime_config(s)

    r_groups = await session.execute(select(SupportGroup))
    groups = list(r_groups.scalars().all())
    groups_by_id = {g.id: g for g in groups}

    r_users = await session.execute(select(BotUser).order_by(BotUser.redmine_id))
    users = [user_orm_to_cfg(u, groups_by_id) for u in r_users.scalars().all()]

    r_st = await session.execute(select(StatusRoomRoute))
    status_map = {row.status_key: row.room_id for row in r_st.scalars().all()}

    r_ver = await session.execute(select(VersionRoomRoute))
    version_map = {row.version_key: row.room_id for row in r_ver.scalars().all()}

    return users, status_map, version_map


async def row_counts(session: AsyncSession | None = None) -> tuple[int, int, int]:
    if session is None:
        factory = get_session_factory()
        async with factory() as s:
            return await row_counts(s)
    nu = await session.scalar(select(func.count()).select_from(BotUser))
    ns = await session.scalar(select(func.count()).select_from(StatusRoomRoute))
    nv = await session.scalar(select(func.count()).select_from(VersionRoomRoute))
    return int(nu or 0), int(ns or 0), int(nv or 0)
