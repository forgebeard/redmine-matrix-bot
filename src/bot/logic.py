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

STATUS_NEW = "Новая"
STATUS_INFO_PROVIDED = "Информация предоставлена"
STATUS_REOPENED = "Открыто повторно"
STATUS_RV = "Передано в работу.РВ"

STATUSES_TRANSFERRED = {
    "Передано в работу.РВ",
    "Передано в работу.РА.Стд",
    "Передано в работу.РА.Пром",
    "Передано в работу.РБД",
    "Передано в работу.ВРМ",
}

NOTIFICATION_TYPES: dict[str, tuple[str, str]] = {
    "new": ("🆕", "Новая задача"),
    "info": ("✅", "Информация предоставлена"),
    "reminder": ("⏰", "Напоминание"),
    "overdue": ("⚠️", "Просроченная задача"),
    "status_change": ("🔄", "Смена статуса"),
    "issue_updated": ("📝", "Задача обновлена"),
    "reopened": ("🔁", "Открыто повторно"),
}

# Имена статусов по ID
STATUS_NAMES: dict[str, str] = {
    "1": "Новая",
    "2": "В работе",
    "5": "Завершена",
    "6": "Отклонена",
    "8": "Ожидание",
    "12": "Запрос информации",
    "13": "Информация предоставлена",
    "17": "Ожидается решение",
    "18": "Открыто повторно",
    "22": "Передано в работу.РВ",
    "23": "Передано в работу.РБД",
    "25": "Передано в работу.РА.Стд",
    "26": "Передано в работу.РА.Пром",
    "27": "Проектирование",
    "28": "Передано в работу.ВРМ",
    "29": "Приостановлено",
    "30": "Передано на L2",
    "31": "Эскалация",
    "32": "Решен",
    "33": "Возвращен (L1)",
}

PRIORITY_NAMES: dict[str, str] = {
    "1": "4 (Низкий)",
    "2": "3 (Нормальный)",
    "3": "2 (Высокий)",
    "4": "1 (Аварийный)",
}

# Приоритет «Аварийный» — пробивает DND и выходные
PRIORITY_EMERGENCY = "1 (Аварийный)"

ID_FIELD_RESOLVERS: dict[str, dict[str, str]] = {
    "status_id": STATUS_NAMES,
    "priority_id": PRIORITY_NAMES,
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
    return "all" in notify_list or notification_type in notify_list


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


def resolve_field_value(field_name: str, value: Any, catalogs=None) -> str:
    """Переводит ID в человекочитаемое имя для известных полей."""
    if value is None or value == "":
        return "—"
    
    # Если переданы каталоги, используем их
    if catalogs is not None:
        if field_name == "status_id":
            return catalogs.status_name(int(value), default=str(value))
        elif field_name == "priority_id":
            return catalogs.priority_name(int(value), default=str(value))
    
    # Fallback на старые словари (если каталоги не загружены)
    resolver = ID_FIELD_RESOLVERS.get(field_name)
    if resolver:
        return resolver.get(str(value), str(value))
    return str(value)


def describe_journal(journal: IssueJournal, skip_status: bool = False) -> str | None:
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

            old_val = resolve_field_value(prop, detail.get("old_value"))
            new_val = resolve_field_value(prop, detail.get("new_value"))
            parts.append(f"{field_label}: {old_val} → {new_val}")
    except Exception:
        pass

    return "; ".join(parts) if parts else None
