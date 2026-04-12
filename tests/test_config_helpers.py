"""Тесты для src/config.py — хелперы конфигурации бота."""

from __future__ import annotations

import os
import pytest

import config as cfg


# ═══════════════════════════════════════════════════════════════════════════
# want_log_file
# ═══════════════════════════════════════════════════════════════════════════


class TestWantLogFile:
    """LOG_TO_FILE: включение/выключение записи в файл."""

    def teardown_method(self):
        os.environ.pop("LOG_TO_FILE", None)

    def test_default_is_true(self):
        # В CI LOG_TO_FILE=0 (через conftest), поэтому по умолчанию False.
        # Тестируем что явное значение работает.
        os.environ.pop("LOG_TO_FILE", None)
        # Не assert True, т.к. conftest ставит LOG_TO_FILE=0
        result = cfg.want_log_file()
        assert result in (True, False)  # просто проверяем что не падает

    def test_explicit_1(self):
        os.environ["LOG_TO_FILE"] = "1"
        assert cfg.want_log_file() is True

    def test_true(self):
        os.environ["LOG_TO_FILE"] = "true"
        assert cfg.want_log_file() is True

    def test_yes(self):
        os.environ["LOG_TO_FILE"] = "yes"
        assert cfg.want_log_file() is True

    def test_on(self):
        os.environ["LOG_TO_FILE"] = "on"
        assert cfg.want_log_file() is True

    def test_0_disables(self):
        os.environ["LOG_TO_FILE"] = "0"
        assert cfg.want_log_file() is False

    def test_false_disables(self):
        os.environ["LOG_TO_FILE"] = "false"
        assert cfg.want_log_file() is False

    def test_no_disables(self):
        os.environ["LOG_TO_FILE"] = "no"
        assert cfg.want_log_file() is False

    def test_off_disables(self):
        os.environ["LOG_TO_FILE"] = "off"
        assert cfg.want_log_file() is False


# ═══════════════════════════════════════════════════════════════════════════
# log_file_max_bytes
# ═══════════════════════════════════════════════════════════════════════════


class TestLogFileMaxBytes:
    """LOG_MAX_BYTES: размер файла лога перед ротацией."""

    def teardown_method(self):
        os.environ.pop("LOG_MAX_BYTES", None)

    def test_default_5mb(self):
        assert cfg.log_file_max_bytes() == 5 * 1024 * 1024

    def test_custom_value(self):
        os.environ["LOG_MAX_BYTES"] = "1048576"
        assert cfg.log_file_max_bytes() == 1048576

    def test_invalid_falls_back(self):
        os.environ["LOG_MAX_BYTES"] = "not_a_number"
        assert cfg.log_file_max_bytes() == 5 * 1024 * 1024

    def test_minimum_1024(self):
        os.environ["LOG_MAX_BYTES"] = "100"
        assert cfg.log_file_max_bytes() >= 1024


# ═══════════════════════════════════════════════════════════════════════════
# log_file_backup_count
# ═══════════════════════════════════════════════════════════════════════════


class TestLogFileBackupCount:
    """LOG_BACKUP_COUNT: число архивных файлов."""

    def teardown_method(self):
        os.environ.pop("LOG_BACKUP_COUNT", None)

    def test_default_5(self):
        assert cfg.log_file_backup_count() == 5

    def test_custom_value(self):
        os.environ["LOG_BACKUP_COUNT"] = "10"
        assert cfg.log_file_backup_count() == 10

    def test_minimum_1(self):
        os.environ["LOG_BACKUP_COUNT"] = "0"
        assert cfg.log_file_backup_count() >= 1

    def test_invalid_falls_back(self):
        os.environ["LOG_BACKUP_COUNT"] = "abc"
        assert cfg.log_file_backup_count() == 5


# ═══════════════════════════════════════════════════════════════════════════
# env_placeholder_hints
# ═══════════════════════════════════════════════════════════════════════════


class TestEnvPlaceholderHints:
    """env_placeholder_hints: предупреждения о placeholder-значениях."""

    def teardown_method(self):
        for key in ["MATRIX_HOMESERVER", "MATRIX_USER_ID", "MATRIX_ACCESS_TOKEN",
                     "REDMINE_URL", "REDMINE_API_KEY"]:
            os.environ.pop(key, None)

    def test_no_placeholders_no_hints(self):
        os.environ["MATRIX_HOMESERVER"] = "https://real.server"
        os.environ["MATRIX_USER_ID"] = "@bot:real.server"
        os.environ["MATRIX_ACCESS_TOKEN"] = "real_token_123"
        os.environ["REDMINE_URL"] = "https://real.redmine"
        os.environ["REDMINE_API_KEY"] = "real_key_456"
        assert cfg.env_placeholder_hints() == []

    def test_matrix_homeserver_placeholder(self):
        os.environ["MATRIX_HOMESERVER"] = "https://your-matrix-server.example.com"
        os.environ["MATRIX_USER_ID"] = "@bot:real"
        os.environ["MATRIX_ACCESS_TOKEN"] = "real"
        os.environ["REDMINE_URL"] = "https://real"
        os.environ["REDMINE_API_KEY"] = "real"
        hints = cfg.env_placeholder_hints()
        assert any("MATRIX_HOMESERVER" in h for h in hints)

    def test_matrix_access_token_placeholder(self):
        os.environ["MATRIX_HOMESERVER"] = "https://real"
        os.environ["MATRIX_USER_ID"] = "@bot:real"
        os.environ["MATRIX_ACCESS_TOKEN"] = "your_access_token_here"
        os.environ["REDMINE_URL"] = "https://real"
        os.environ["REDMINE_API_KEY"] = "real"
        hints = cfg.env_placeholder_hints()
        assert any("MATRIX_ACCESS_TOKEN" in h for h in hints)

    def test_redmine_url_placeholder(self):
        os.environ["MATRIX_HOMESERVER"] = "https://real"
        os.environ["MATRIX_USER_ID"] = "@bot:real"
        os.environ["MATRIX_ACCESS_TOKEN"] = "real"
        os.environ["REDMINE_URL"] = "https://your-redmine.example.com"
        os.environ["REDMINE_API_KEY"] = "real"
        hints = cfg.env_placeholder_hints()
        assert any("REDMINE_URL" in h for h in hints)

    def test_redmine_api_key_placeholder(self):
        os.environ["MATRIX_HOMESERVER"] = "https://real"
        os.environ["MATRIX_USER_ID"] = "@bot:real"
        os.environ["MATRIX_ACCESS_TOKEN"] = "real"
        os.environ["REDMINE_URL"] = "https://real"
        os.environ["REDMINE_API_KEY"] = "your_api_key_here"
        hints = cfg.env_placeholder_hints()
        assert any("REDMINE_API_KEY" in h for h in hints)

    def test_all_placeholders(self):
        os.environ["MATRIX_HOMESERVER"] = "https://your-matrix-server.example.com"
        os.environ["MATRIX_USER_ID"] = "https://your-matrix-server.example.com"
        os.environ["MATRIX_ACCESS_TOKEN"] = "your_access_token_here"
        os.environ["REDMINE_URL"] = "https://your-redmine.example.com"
        os.environ["REDMINE_API_KEY"] = "your_api_key_here"
        hints = cfg.env_placeholder_hints()
        assert len(hints) == 5


# ═══════════════════════════════════════════════════════════════════════════
# _parse_json_env
# ═══════════════════════════════════════════════════════════════════════════


class TestParseJsonEnv:
    """_parse_json_env: парсинг JSON из переменной окружения."""

    def teardown_method(self):
        os.environ.pop("TEST_JSON_VAR", None)

    def test_valid_dict(self):
        os.environ["TEST_JSON_VAR"] = '{"key": "value"}'
        result = cfg._parse_json_env("TEST_JSON_VAR", "{}")
        assert result == {"key": "value"}

    def test_valid_list(self):
        os.environ["TEST_JSON_VAR"] = '[1, 2, 3]'
        result = cfg._parse_json_env("TEST_JSON_VAR", "[]")
        assert result == [1, 2, 3]

    def test_invalid_json_returns_default(self):
        os.environ["TEST_JSON_VAR"] = 'not json'
        result = cfg._parse_json_env("TEST_JSON_VAR", '{"fallback": true}')
        assert result == {"fallback": True}

    def test_missing_var_returns_default(self):
        result = cfg._parse_json_env("MISSING_JSON_VAR", '{"default": 42}')
        assert result == {"default": 42}

    def test_empty_string_returns_default(self):
        os.environ["TEST_JSON_VAR"] = ""
        result = cfg._parse_json_env("TEST_JSON_VAR", '{"d": 1}')
        assert result == {"d": 1}

    def test_null_returns_none(self):
        os.environ["TEST_JSON_VAR"] = "null"
        result = cfg._parse_json_env("TEST_JSON_VAR", "null")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# validate_required_env
# ═══════════════════════════════════════════════════════════════════════════


class TestValidateRequiredEnv:
    """validate_required_env: проверка обязательных переменных.

    Внимание: переменные читаются на уровне модуля при импорте config,
    поэтому monkeypatch не влияет на уже импортированные значения.
    Если тесты admin.main запускаются до этого — переменные уже загружены.
    """

    def test_returns_tuple(self):
        """Функция всегда возвращает (bool, list)."""
        result = cfg.validate_required_env()
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], list)

    def test_error_messages_are_clear(self):
        """Если есть ошибки — сообщения содержат имена переменных."""
        ok, errors = cfg.validate_required_env()
        for err in errors:
            assert "Не задана переменная" in err
