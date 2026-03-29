"""
Тесты src/preferences.py: рабочие часы, выходные, DND, can_notify.

Модуль пока не подключён к bot.py — тесты фиксируют желаемое поведение на будущее.
"""

from datetime import datetime, time

import pytest
from preferences import (
    can_notify,
    get_work_days,
    get_work_hours,
    is_dnd,
    is_working_time,
    DEFAULT_WORK_START,
    DEFAULT_WORK_END,
    DEFAULT_WORK_DAYS,
)
from config import PRIORITY_EMERGENCY
from utils import BOT_TZ


def _dt(hour, minute=0, weekday=0):
    """Создаёт datetime с нужным часом и днём недели (0=Пн)."""
    # 2025-07-07 — понедельник
    from datetime import timedelta
    base = datetime(2025, 7, 7, hour, minute, tzinfo=BOT_TZ)  # Пн
    return base + timedelta(days=weekday)


# ═══════════════════════════════════════════════════════════════
# get_work_hours
# ═══════════════════════════════════════════════════════════════

class TestGetWorkHours:

    def test_default(self):
        s, e = get_work_hours({})
        assert s == DEFAULT_WORK_START
        assert e == DEFAULT_WORK_END

    def test_custom(self):
        s, e = get_work_hours({"work_hours": "10:00-20:00"})
        assert s == time(10, 0)
        assert e == time(20, 0)

    def test_invalid_format(self):
        s, e = get_work_hours({"work_hours": "invalid"})
        assert s == DEFAULT_WORK_START

    def test_empty_string(self):
        s, e = get_work_hours({"work_hours": ""})
        assert s == DEFAULT_WORK_START

    def test_with_spaces(self):
        s, e = get_work_hours({"work_hours": " 08:30 - 17:30 "})
        assert s == time(8, 30)
        assert e == time(17, 30)


# ═══════════════════════════════════════════════════════════════
# get_work_days
# ═══════════════════════════════════════════════════════════════

class TestGetWorkDays:

    def test_default(self):
        assert get_work_days({}) == DEFAULT_WORK_DAYS

    def test_custom(self):
        assert get_work_days({"work_days": [0, 1, 2]}) == {0, 1, 2}

    def test_none_returns_default(self):
        assert get_work_days({"work_days": None}) == DEFAULT_WORK_DAYS

    def test_invalid_type_returns_default(self):
        assert get_work_days({"work_days": "monday"}) == DEFAULT_WORK_DAYS


# ═══════════════════════════════════════════════════════════════
# is_working_time
# ═══════════════════════════════════════════════════════════════

class TestIsWorkingTime:

    def test_midday_weekday(self):
        dt = _dt(12, 0, weekday=0)  # Пн 12:00
        assert is_working_time({}, dt) is True

    def test_before_work(self):
        dt = _dt(7, 0, weekday=0)  # Пн 07:00
        assert is_working_time({}, dt) is False

    def test_after_work(self):
        dt = _dt(19, 0, weekday=0)  # Пн 19:00
        assert is_working_time({}, dt) is False

    def test_weekend(self):
        dt = _dt(12, 0, weekday=5)  # Сб 12:00
        assert is_working_time({}, dt) is False

    def test_sunday(self):
        dt = _dt(12, 0, weekday=6)  # Вс 12:00
        assert is_working_time({}, dt) is False

    def test_custom_hours(self):
        cfg = {"work_hours": "10:00-22:00"}
        dt = _dt(21, 0, weekday=0)
        assert is_working_time(cfg, dt) is True

    def test_exact_start(self):
        dt = _dt(9, 0, weekday=0)
        assert is_working_time({}, dt) is True

    def test_exact_end(self):
        dt = _dt(18, 0, weekday=0)
        assert is_working_time({}, dt) is True


# ═══════════════════════════════════════════════════════════════
# is_dnd
# ═══════════════════════════════════════════════════════════════

class TestIsDnd:

    def test_dnd_true(self):
        assert is_dnd({"dnd": True}) is True

    def test_dnd_false(self):
        assert is_dnd({"dnd": False}) is False

    def test_dnd_missing(self):
        assert is_dnd({}) is False

    def test_dnd_string_not_bool(self):
        assert is_dnd({"dnd": "yes"}) is False


# ═══════════════════════════════════════════════════════════════
# can_notify
# ═══════════════════════════════════════════════════════════════

class TestCanNotify:

    def test_emergency_bypasses_dnd(self):
        assert can_notify({"dnd": True}, PRIORITY_EMERGENCY) is True

    def test_emergency_bypasses_weekend(self):
        dt = _dt(12, 0, weekday=5)  # Сб
        assert can_notify({}, PRIORITY_EMERGENCY, dt) is True

    def test_dnd_blocks_normal(self):
        dt = _dt(12, 0, weekday=0)  # Рабочее время
        assert can_notify({"dnd": True}, "", dt) is False

    def test_working_time_allows(self):
        dt = _dt(12, 0, weekday=0)
        assert can_notify({}, "", dt) is True

    def test_outside_hours_blocks(self):
        dt = _dt(23, 0, weekday=0)
        assert can_notify({}, "", dt) is False

    def test_weekend_blocks_normal(self):
        dt = _dt(12, 0, weekday=6)
        assert can_notify({}, "", dt) is False