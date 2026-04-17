"""
Чистая бизнес-логика бота — без I/O, без async, без глобальных мутаций.

Эти функции легко тестировать: принимают аргументы → возвращают результат.
Никаких HTTP-запросов, DB-сессий, logger вызовов.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from typing import Protocol

    from redminelib.resources import IssueJournal

    class _IssueLike(Protocol):
        id: int
        status: Any  # .name
        priority: Any  # .name
        subject: str
        due_date: Any  # date | None
        fixed_version: Any  # .name | None
        journals: Any  # iterable

else:
    _IssueLike = Any

# ═══════════════════════════════════════════════════════════════════════════
# КОНСТАНТЫ
# ═══════════════════════════════════════════════════════════════════════════

# Legacy status names (used by scheduler/main/config compatibility layer)
STATUS_NEW = "Новая"
STATUS_INFO_PROVIDED = "Информация предоставлена"
STATUS_REOPENED = "Открыта повторно"
STATUS_RV = "Передано в работу.РВ"
STATUSES_TRANSFERRED = {STATUS_RV}

# Compatibility exports for config/sender legacy usage.
STATUS_NAMES = {
    "new": STATUS_NEW,
    "info_provided": STATUS_INFO_PROVIDED,
    "reopened": STATUS_REOPENED,
    "transferred": STATUS_RV,
}
PRIORITY_NAMES: dict[str, str] = {}
PRIORITY_EMERGENCY = "Аварийный"
NOTIFICATION_TYPES = {
    "new": ("🆕", "Новая задача"),
    "reopened": ("♻️", "Задача открыта повторно"),
    "info": ("ℹ️", "Информация предоставлена"),
    "reminder": ("⏰", "Напоминание"),
    "overdue": ("⚠️", "Просроченная задача"),
    "issue_updated": ("📝", "Задача обновлена"),
    "status_change": ("🔁", "Смена статуса"),
}

FIELD_NAMES: dict[str, str | None] = {
    "status_id": "Статус",
    "assigned_to_id": "Назначена",
    "priority_id": "Приоритет",
    "done_ratio": "Готовность",
    "due_date": "Срок",
    "subject": "Тема",
    "description": None,
    "tracker_id": "Трекер",
    "fixed_version_id": "Версия",
    "project_id": "Проект",
    "category_id": "Категория",
    "parent_id": "Родительская",
    "start_date": "Дата начала",
    "estimated_hours": "Оценка часов",
}

HIDDEN_FIELDS_PATTERN = re.compile(r"^\d+$")


# ═══════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════


def plural_days(n: int) -> str:
    """Склонение слова 'день': 1 день, 2 дня, 5 дней."""
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def ensure_tz(dt: datetime, tz: ZoneInfo) -> datetime:
    """Гарантирует наличие таймзоны у datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt


def get_version_name(issue: _IssueLike) -> str | None:
    """Получает название версии задачи (или None)."""
    try:
        return issue.fixed_version.name
    except Exception:
        return None


def should_notify(user_cfg: dict[str, Any], notification_type: str) -> bool:
    """
    Проверяет, подписан ли пользователь на данный тип уведомлений.
    "all" — подписан на всё.
    """
    notify_list = user_cfg.get("notify", ["all"])
    norm = {str(v).strip().lower() for v in (notify_list or []) if str(v).strip()}
    if not norm or "all" in norm:
        return True
    known_types = {k.lower() for k in NOTIFICATION_TYPES}
    # Backward-compat: when notify stores status filters (ids/names), do not
    # block delivery by notification kind here; filtering happens in issue_matches_cfg.
    if any(v not in known_types for v in norm):
        return True
    return notification_type.lower() in norm


def _issue_priority_name(issue) -> str:
    try:
        return issue.priority.name
    except Exception:
        return ""


def validate_users(users: list[dict]) -> tuple[bool, list[str]]:
    """
    Проверяет, что у каждого пользователя есть обязательные поля.
    Возвращает (ok, errors).
    """
    errors: list[str] = []
    required_fields = ("redmine_id", "room")
    for i, u in enumerate(users):
        for field in required_fields:
            if field not in u:
                errors.append(f"USERS[{i}]: отсутствует обязательное поле '{field}'")
        if "redmine_id" in u and not isinstance(u["redmine_id"], int):
            errors.append(
                f"USERS[{i}]: 'redmine_id' должен быть int, получено {type(u['redmine_id']).__name__}"
            )
        if "room" in u and (not isinstance(u["room"], str) or not u["room"].strip()):
            errors.append(f"USERS[{i}]: 'room' должен быть непустой строкой")
        if "notify" in u and not isinstance(u["notify"], list):
            errors.append(
                f"USERS[{i}]: 'notify' должен быть списком, получено {type(u['notify']).__name__}"
            )
    return len(errors) == 0, errors


# ═══════════════════════════════════════════════════════════════════════════
# РОУТИНГ: какие доп. комнаты получают уведомление
# ═══════════════════════════════════════════════════════════════════════════

_LEGACY_VERSION_FALLBACK_KEY = "РЕД ОС"


def _extra_rooms_for_issue_version(
    issue: _IssueLike,
    user_cfg: dict[str, Any],
    version_room_map: dict[str, str],
    users: list[dict[str, Any]],
) -> set[str]:
    """
    Доп. комнаты по названию версии задачи в Redmine.
    """
    rooms: set[str] = set()
    version_name = get_version_name(issue) or ""
    if not version_name.strip():
        r = (version_room_map.get(_LEGACY_VERSION_FALLBACK_KEY) or "").strip()
        return {r} if r else set()
    vn = version_name.lower()
    for spec in user_cfg.get("version_routes") or []:
        key = (spec.get("key") or "").strip()
        rid = (spec.get("room") or "").strip()
        if key and rid and key.lower() in vn:
            rooms.add(rid)
    for key, room in (version_room_map or {}).items():
        r = (room or "").strip()
        if not r:
            continue
        k = (key or "").strip()
        if k and k.lower() in vn:
            rooms.add(r)
    return rooms


def get_extra_rooms_for_new(
    issue: _IssueLike,
    user_cfg: dict[str, Any],
    version_room_map: dict[str, str],
    users: list[dict[str, Any]],
) -> set[str]:
    """Доп. комнаты для НОВОЙ задачи — по версии и глобальным маршрутам."""
    return _extra_rooms_for_issue_version(issue, user_cfg, version_room_map, users)


def get_extra_rooms_for_rv(
    issue: _IssueLike,
    user_cfg: dict[str, Any],
    status_room_map: dict[str, str],
    version_room_map: dict[str, str],
    users: list[dict[str, Any]],
) -> set[str]:
    """Доп. комнаты для статуса «Передано в работу.РВ»."""
    rooms: set[str] = set()
    rv_room = status_room_map.get(STATUS_RV)
    if rv_room:
        rooms.add(rv_room)
    rooms |= _extra_rooms_for_issue_version(issue, user_cfg, version_room_map, users)
    return rooms


def _group_member_rooms(
    user_cfg: dict[str, Any],
    users: list[dict[str, Any]],
) -> set[str]:
    """Личные комнаты участников той же группы."""
    gid = user_cfg.get("group_id")
    if gid is None:
        return set()
    out: set[str] = set()
    for u in users:
        if u.get("group_id") != gid:
            continue
        r = (u.get("room") or "").strip()
        if r:
            out.add(r)
    return out


def _group_room(user_cfg: dict[str, Any]) -> str:
    return (user_cfg.get("group_room") or "").strip()


def _cfg_for_room(
    user_cfg: dict[str, Any],
    room_id: str,
) -> dict[str, Any]:
    """
    Для Matrix-комнаты группы применяются типы уведомлений и расписание группы.
    """
    target = (room_id or "").strip()
    gr = _group_room(user_cfg)
    if not target or not gr or target != gr:
        return user_cfg
    gd = user_cfg.get("group_delivery")
    if not isinstance(gd, dict):
        return user_cfg
    merged = dict(user_cfg)
    merged["notify"] = gd.get("notify") if isinstance(gd.get("notify"), list) else ["all"]
    merged["versions"] = gd.get("versions") if isinstance(gd.get("versions"), list) else ["all"]
    merged["priorities"] = gd.get("priorities") if isinstance(gd.get("priorities"), list) else ["all"]
    wh = gd.get("work_hours")
    if wh:
        merged["work_hours"] = wh
    else:
        merged.pop("work_hours", None)
    wd = gd.get("work_days")
    if wd is not None:
        merged["work_days"] = wd
    else:
        merged.pop("work_days", None)
    merged["dnd"] = bool(gd.get("dnd"))
    return merged


def _matches_filter(filter_values: Any, candidates: set[str]) -> bool:
    values = filter_values if isinstance(filter_values, list) else ["all"]
    norm = {str(v).strip().lower() for v in values if str(v).strip()}
    if not norm or "all" in norm:
        return True
    for c in candidates:
        if c.strip().lower() in norm:
            return True
    return False


def issue_matches_cfg(issue: _IssueLike, user_cfg: dict[str, Any]) -> bool:
    """Attribute matching by status/version/priority for user/group delivery config."""
    status_candidates = {str(getattr(getattr(issue, "status", None), "id", "")).strip()}
    status_name = str(getattr(getattr(issue, "status", None), "name", "")).strip()
    if status_name:
        status_candidates.add(status_name)

    version_candidates: set[str] = set()
    fv = getattr(issue, "fixed_version", None)
    if fv is not None:
        version_id = str(getattr(fv, "id", "")).strip()
        version_name = str(getattr(fv, "name", "")).strip()
        if version_id:
            version_candidates.add(version_id)
        if version_name:
            version_candidates.add(version_name)
    if not version_candidates:
        version_candidates.add("__none__")

    priority_candidates = {str(getattr(getattr(issue, "priority", None), "id", "")).strip()}
    priority_name = str(getattr(getattr(issue, "priority", None), "name", "")).strip()
    if priority_name:
        priority_candidates.add(priority_name)

    raw_notify = user_cfg.get("notify", ["all"])
    notify_norm = {str(v).strip().lower() for v in (raw_notify or []) if str(v).strip()}
    known_types = {k.lower() for k in NOTIFICATION_TYPES}
    # Legacy mode: notify list contains notification kinds, not statuses.
    # In this case skip status attribute matching.
    status_match = True
    if notify_norm and "all" not in notify_norm and any(v not in known_types for v in notify_norm):
        status_match = _matches_filter(raw_notify, status_candidates)

    return (
        status_match
        and _matches_filter(user_cfg.get("versions", ["all"]), version_candidates)
        and _matches_filter(user_cfg.get("priorities", ["all"]), priority_candidates)
    )


# ═══════════════════════════════════════════════════════════════════════════
# ДЕТЕКТОРЫ ИЗМЕНЕНИЙ
# ═══════════════════════════════════════════════════════════════════════════


def detect_status_change(issue: _IssueLike, sent: dict[str, dict]) -> str | None:
    """
    Сравнивает текущий статус задачи с сохранённым.
    Возвращает старый статус если изменился, иначе None.
    """
    issue_id = str(issue.id)
    if issue_id not in sent:
        return None
    old_status = sent[issue_id].get("status")
    if old_status and old_status != issue.status.name:
        return old_status
    return None


def detect_new_journals(
    issue: _IssueLike,
    journals_state: dict[str, Any],
) -> tuple[list[IssueJournal], int]:
    """
    Находит новые записи в журнале задачи.
    Returns: (new_journals, max_journal_id)
    """
    issue_id = str(issue.id)
    last_known_id = journals_state.get(issue_id, {}).get("last_journal_id", 0)

    try:
        all_journals = list(issue.journals)
    except Exception:
        return [], 0

    if not all_journals:
        return [], 0

    max_id = max(j.id for j in all_journals)
    new_journals = [j for j in all_journals if j.id > last_known_id]
    return new_journals, max_id


# ═══════════════════════════════════════════════════════════════════════════
# ОПИСАНИЕ ЖУРНАЛА
# ═══════════════════════════════════════════════════════════════════════════


def resolve_field_value(field_name: str, value: Any, catalogs: BotCatalogs | None = None) -> str:
    """Переводит ID в человекочитаемое имя для известных полей."""
    if value is None or value == "":
        return "—"
    if field_name == "status_id" and catalogs:
        return catalogs.status_name(int(value), default=str(value))
    if field_name == "priority_id" and catalogs:
        return catalogs.priority_name(int(value), default=str(value))
    return str(value)


def describe_journal(journal: IssueJournal, skip_status: bool = False, catalogs: BotCatalogs | None = None) -> str | None:
    """Описывает одну запись журнала в человекочитаемом виде."""
    parts: list[str] = []

    if journal.notes:
        try:
            parts.append(f"💬 Комментарий от {journal.user.name}")
        except Exception:
            parts.append("💬 Новый комментарий")

    try:
        for detail in journal.details:
            prop = detail.get("name", detail.get("property", "?"))

            if HIDDEN_FIELDS_PATTERN.match(prop):
                continue
            if prop == "status_id" and skip_status:
                continue

            field_label = FIELD_NAMES.get(prop)
            if field_label is None:
                continue

            old_val = resolve_field_value(prop, detail.get("old_value"), catalogs)
            new_val = resolve_field_value(prop, detail.get("new_value"), catalogs)
            parts.append(f"{field_label}: {old_val} → {new_val}")
    except Exception:
        pass

    return "; ".join(parts) if parts else None
