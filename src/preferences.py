"""
Пользовательские предпочтения: рабочие часы, DND, выходные.

Определяет, можно ли отправлять уведомление пользователю
в текущий момент времени (can_notify).

Вызывается из bot.send_safe: DND, рабочие часы и дни; приоритет
«Аварийный» пробивает ограничения (определяется через каталоги).
"""

from datetime import time

from bot.config_state import CATALOGS
from utils import now_tz

# ═══════════════════════════════════════════════════════════════
# ДЕФОЛТЫ
# ═══════════════════════════════════════════════════════════════

DEFAULT_WORK_START = time(9, 0)
DEFAULT_WORK_END = time(18, 0)
DEFAULT_WORK_DAYS = {0, 1, 2, 3, 4}  # Пн-Пт


def get_work_hours(user_cfg: dict) -> tuple[time, time]:
    """
    Возвращает (start, end) рабочих часов пользователя.
    Формат в конфиге: "work_hours": "09:00-18:00"
    """
    raw = user_cfg.get("work_hours", "")
    if not raw or "-" not in raw:
        return DEFAULT_WORK_START, DEFAULT_WORK_END

    try:
        start_s, end_s = raw.split("-", 1)
        sh, sm = map(int, start_s.strip().split(":"))
        eh, em = map(int, end_s.strip().split(":"))
        return time(sh, sm), time(eh, em)
    except (ValueError, TypeError):
        return DEFAULT_WORK_START, DEFAULT_WORK_END


def get_work_days(user_cfg: dict) -> set[int]:
    """
    Возвращает множество рабочих дней недели (0=Пн, 6=Вс).
    Формат в конфиге: "work_days": [0, 1, 2, 3, 4]
    """
    days = user_cfg.get("work_days")
    if days is None:
        return DEFAULT_WORK_DAYS
    if isinstance(days, list):
        return set(days)
    return DEFAULT_WORK_DAYS


def is_working_time(user_cfg: dict, dt=None) -> bool:
    """
    Проверяет, попадает ли текущее (или переданное) время
    в рабочие часы пользователя.
    """
    if dt is None:
        dt = now_tz()

    # Выходной?
    work_days = get_work_days(user_cfg)
    if dt.weekday() not in work_days:
        return False

    # Рабочие часы?
    start, end = get_work_hours(user_cfg)
    return start <= dt.time() <= end


def is_dnd(user_cfg: dict) -> bool:
    """Ручной режим DND — пользователь выключил уведомления."""
    return user_cfg.get("dnd", False) is True


def can_notify(user_cfg: dict, priority: str = "", dt=None) -> bool:
    """
    Главная функция: можно ли отправить уведомление.

    Аварийный приоритет пробивает DND и выходные.
    """
    # Аварийный — всегда
    if CATALOGS and CATALOGS.is_emergency(priority_name=priority):
        return True

    # Ручной DND
    if is_dnd(user_cfg):
        return False

    # Рабочее время
    return is_working_time(user_cfg, dt)
