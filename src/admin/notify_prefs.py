"""Парсинг и нормализация настроек уведомлений и рабочего времени (формы пользователей и /me/settings)."""

from __future__ import annotations

import json

from admin.constants import NOTIFY_TYPE_KEYS


def parse_notify(raw: str) -> list:
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else ["all"]
    except json.JSONDecodeError:
        return ["all"]


def normalize_notify(values: list[str] | None) -> list[str]:
    vals = [v.strip() for v in (values or []) if v and v.strip()]
    if not vals:
        return ["all"]
    if "all" in vals:
        return ["all"]
    allowed = [v for v in vals if v in NOTIFY_TYPE_KEYS]
    return allowed or ["all"]


def notify_preset(notify: list | None) -> str:
    data = normalize_notify([str(x) for x in (notify or [])])
    if "all" in data:
        return "all"
    if set(data) == {"new"}:
        return "new_only"
    if set(data) == {"overdue"}:
        return "overdue_only"
    return "custom"


def parse_work_days(raw: str) -> list[int] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else None
    except json.JSONDecodeError:
        return None


def parse_work_hours_range(value: str) -> tuple[str, str]:
    if not value or "-" not in value:
        return "", ""
    start, end = value.split("-", 1)
    return start.strip(), end.strip()
