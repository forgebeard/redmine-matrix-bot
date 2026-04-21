"""
Тесты модуля src/config.py (без запуска бота).

Проверяем: validate_users, validate_required_env, should_notify — то, что
нужно при старте, чтобы не падать на кривом USERS в .env.
"""

import config
from config import should_notify, validate_required_env, validate_users

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
        ok, errs = validate_users(
            [
                {
                    "redmine_id": 1972,
                    "room": "!abc:server",
                    "notify": ["all"],
                }
            ]
        )
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
        ok, errs = validate_users(
            [
                {
                    "redmine_id": 1,
                    "room": "!abc:server",
                    "notify": "all",
                }
            ]
        )
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


# ═══════════════════════════════════════════════════════════════
# Логи: LOG_TO_FILE, LOG_PATH
# ═══════════════════════════════════════════════════════════════


class TestLogPaths:
    def test_want_log_file_default(self, monkeypatch):
        monkeypatch.delenv("LOG_TO_FILE", raising=False)
        from config import want_log_file

        assert want_log_file() is True

    def test_want_log_file_disabled(self, monkeypatch):
        monkeypatch.setenv("LOG_TO_FILE", "0")
        from config import want_log_file

        assert want_log_file() is False

    def test_resolved_log_file_default(self, monkeypatch):
        monkeypatch.delenv("LOG_PATH", raising=False)
        from config import BASE_DIR, resolved_log_file

        assert resolved_log_file() == BASE_DIR / "data" / "bot.log"

    def test_resolved_log_file_relative(self, monkeypatch):
        monkeypatch.setenv("LOG_PATH", "logs/app.log")
        from config import BASE_DIR, resolved_log_file

        assert resolved_log_file() == BASE_DIR / "logs" / "app.log"

    def test_log_file_max_bytes_default(self, monkeypatch):
        monkeypatch.delenv("LOG_MAX_BYTES", raising=False)
        from config import log_file_max_bytes

        assert log_file_max_bytes() == 5 * 1024 * 1024

    def test_log_file_max_bytes_custom(self, monkeypatch):
        monkeypatch.setenv("LOG_MAX_BYTES", "1048576")
        from config import log_file_max_bytes

        assert log_file_max_bytes() == 1048576

    def test_log_file_backup_count_default(self, monkeypatch):
        monkeypatch.delenv("LOG_BACKUP_COUNT", raising=False)
        from config import log_file_backup_count

        assert log_file_backup_count() == 5

    def test_log_file_backup_count_clamped_to_min_one(self, monkeypatch):
        monkeypatch.setenv("LOG_BACKUP_COUNT", "0")
        from config import log_file_backup_count

        assert log_file_backup_count() == 1


class TestEnvPlaceholderHints:
    def test_clean_env_no_hints(self, monkeypatch):
        monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.example.org")
        monkeypatch.setenv("MATRIX_USER_ID", "@bot:example.org")
        monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "secret")
        monkeypatch.setenv("REDMINE_URL", "https://rm.example.org")
        monkeypatch.setenv("REDMINE_API_KEY", "key")
        from config import env_placeholder_hints

        assert env_placeholder_hints() == []

    def test_detects_example_matrix_user(self, monkeypatch):
        monkeypatch.setenv("MATRIX_USER_ID", "@bot:your-matrix-server.example.com")
        from config import env_placeholder_hints

        assert any("MATRIX_USER_ID" in h for h in env_placeholder_hints())

    def test_detects_example_token(self, monkeypatch):
        monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "your_access_token_here")
        from config import env_placeholder_hints

        assert any("MATRIX_ACCESS_TOKEN" in h for h in env_placeholder_hints())


class TestV5Config:
    def test_v5_defaults(self):
        assert config.POLLING_INTERVAL_SEC >= 15
        assert config.DEDUP_TTL_HOURS >= 1
        assert config.SUBJECT_MAX_LEN >= 32

    def test_validate_required_env_allows_empty_portal(self, monkeypatch):
        monkeypatch.setattr(config, "MATRIX_HOMESERVER", "https://mx")
        monkeypatch.setattr(config, "MATRIX_ACCESS_TOKEN", "tok")
        monkeypatch.setattr(config, "MATRIX_USER_ID", "@bot:mx")
        monkeypatch.setattr(config, "REDMINE_URL", "https://rm")
        monkeypatch.setattr(config, "REDMINE_API_KEY", "rk")
        monkeypatch.setattr(config, "PORTAL_BASE_URL", "")
        ok, errors = validate_required_env()
        assert ok
        assert errors == []
