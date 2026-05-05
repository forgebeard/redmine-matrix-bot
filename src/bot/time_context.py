"""
Контекст уведомлений: личный (DM) vs комната группы.

Таймзона сервиса (utils.BOT_TZ) — fallback, если у пользователя/группы зона не задана.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import utils

NotifyContext = Literal["personal", "group_room"]


def resolve_effective_zone(user_cfg: dict[str, Any], *, context: NotifyContext) -> ZoneInfo:
    svc = utils.BOT_TZ
    if context == "group_room":
        tz_name = (user_cfg.get("group_timezone") or user_cfg.get("timezone") or "").strip()
        if tz_name:
            return ZoneInfo(tz_name)
        return svc
    tz_name = (user_cfg.get("timezone") or "").strip()
    if tz_name:
        return ZoneInfo(tz_name)
    return svc


def now_in_notify_context(user_cfg: dict[str, Any], *, context: NotifyContext) -> datetime:
    return datetime.now(tz=resolve_effective_zone(user_cfg, context=context))


def notify_context_for_room(user_cfg: dict[str, Any], room_id: str) -> NotifyContext:
    """Личная комната пользователя — personal; комната группы (или запись GROUPS) — group_room."""
    r = (room_id or "").strip()
    gr = (user_cfg.get("group_room") or "").strip()
    if gr and r == gr:
        return "group_room"
    room_field = (user_cfg.get("room") or "").strip()
    if room_field and r == room_field and user_cfg.get("group_id") is not None and not gr:
        return "group_room"
    return "personal"
