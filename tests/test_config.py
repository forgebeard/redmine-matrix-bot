"""
Тесты модуля src/config.py (без запуска бота).

Проверяем: validate_users, validate_required_env, should_notify — то, что
нужно при старте, чтобы не падать на кривом USERS в .env.
"""

import pytest
from config import validate_users, should_notify, validate_required_env


# ═══════════════════════════════════════════════════════════════
# validate_users
# ═══════════════════════════════════════════════════════════════

class TestValidateUsers:
    """Валидация структуры USERS."""

    def test_valid_minimal(self):
        ok, errs = validate_users([{"redmine_id": 1, "room": "!abc:server"}])
        assert ok is True
        assert errs == []

    def test_valid_full(self):
        ok, errs = validate_users([{
            "redmine_id": 1972,
            "room": "!abc:server",
            "notify": ["all"],
        }])
        assert ok is True

    def test_missing_redmine_id(self):
        ok, errs = validate_users([{"room": "!abc:server"}])
        assert ok is False
        assert any("redmine_id" in e for e in errs)

    def test_missing_room(self):
        ok, errs = validate_users([{"redmine_id": 1}])
        assert ok is False
        assert any("room" in e for e in errs)

    def test_redmine_id_not_int(self):
        ok, errs = validate_users([{"redmine_id": "1972", "room": "!abc:server"}])
        assert ok is False
        assert any("int" in e for e in errs)

    def test_room_empty_string(self):
        ok, errs = validate_users([{"redmine_id": 1, "room": ""}])
        assert ok is False

    def test_room_whitespace(self):
        ok, errs = validate_users([{"redmine_id": 1, "room": "   "}])
        assert ok is False

    def test_notify_not_list(self):
        ok, errs = validate_users([{
            "redmine_id": 1,
            "room": "!abc:server",
            "notify": "all",
        }])
        assert ok is False
        assert any("списком" in e for e in errs)

    def test_multiple_users_one_invalid(self):
        users = [
            {"redmine_id": 1, "room": "!abc:server"},
            {"redmine_id": "bad", "room": "!def:server"},
        ]
        ok, errs = validate_users(users)
        assert ok is False
        assert len(errs) == 1
        assert "USERS[1]" in errs[0]

    def test_empty_list(self):
        ok, errs = validate_users([])
        assert ok is True
        assert errs == []


# ═══════════════════════════════════════════════════════════════
# should_notify
# ═══════════════════════════════════════════════════════════════

class TestShouldNotify:
    """Проверка подписки на тип уведомления."""

    def test_all_matches_everything(self):
        cfg = {"notify": ["all"]}
        assert should_notify(cfg, "new") is True
        assert should_notify(cfg, "overdue") is True
        assert should_notify(cfg, "whatever") is True

    def test_specific_types(self):
        cfg = {"notify": ["new", "info"]}
        assert should_notify(cfg, "new") is True
        assert should_notify(cfg, "info") is True
        assert should_notify(cfg, "overdue") is False

    def test_empty_notify(self):
        cfg = {"notify": []}
        assert should_notify(cfg, "new") is False

    def test_missing_notify_defaults_to_all(self):
        cfg = {"redmine_id": 1}
        assert should_notify(cfg, "new") is True

    def test_daily_report_type(self):
        cfg = {"notify": ["new", "daily_report"]}
        assert should_notify(cfg, "daily_report") is True
        assert should_notify(cfg, "overdue") is False