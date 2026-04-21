"""Тесты для src/admin/routes/settings.py — onboarding, check, DB config."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import admin.routes.settings as settings_mod

# ═══════════════════════════════════════════════════════════════════════════
# _mask_secret_value
# ═══════════════════════════════════════════════════════════════════════════


class TestMaskSecretValue:
    """Маскировка секретов: URL и MXID не маскируются, ключи маскируются."""

    def test_unmasked_url(self):
        assert (
            settings_mod._mask_secret_value("REDMINE_URL", "https://red.example.com")
            == "https://red.example.com"
        )

    def test_unmasked_homeserver(self):
        assert (
            settings_mod._mask_secret_value("MATRIX_HOMESERVER", "https://mx.example.com")
            == "https://mx.example.com"
        )

    def test_unmasked_mxid(self):
        assert (
            settings_mod._mask_secret_value("MATRIX_USER_ID", "@bot:example.com")
            == "@bot:example.com"
        )

    def test_masked_api_key_long(self):
        val = "abcdef1234567890abcdef1234567890"
        masked = settings_mod._mask_secret_value("REDMINE_API_KEY", val)
        assert masked.startswith("abcd")
        assert masked.endswith("7890")
        assert "•" in masked
        assert len(masked) == len(val)

    def test_masked_api_key_short(self):
        masked = settings_mod._mask_secret_value("REDMINE_API_KEY", "short")
        assert masked == "••••••••"

    def test_masked_empty(self):
        assert settings_mod._mask_secret_value("REDMINE_API_KEY", "") == "••••••••"

    def test_masked_token_short(self):
        masked = settings_mod._mask_secret_value("MATRIX_ACCESS_TOKEN", "12345678")
        assert masked == "••••••••"

    def test_masked_token_9_chars(self):
        masked = settings_mod._mask_secret_value("MATRIX_ACCESS_TOKEN", "123456789")
        assert masked.startswith("1234")
        assert masked.endswith("6789")
        assert "•" in masked


class TestNormalizeBaseUrl:
    def test_strips_spaces_and_trailing_slash(self):
        assert settings_mod._normalize_base_url("  https://support.red-soft.ru/  ") == (
            "https://support.red-soft.ru"
        )

    def test_empty_value(self):
        assert settings_mod._normalize_base_url("") == ""


# ═══════════════════════════════════════════════════════════════════════════
# _check_redmine_access
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckRedmineAccess:
    """Проверка подключения к Redmine."""

    def test_empty_url(self):
        ok, msg = settings_mod._check_redmine_access("", "somekey")
        assert not ok
        assert "укажите URL" in msg

    def test_empty_key(self):
        ok, msg = settings_mod._check_redmine_access("https://red.example.com", "")
        assert not ok
        assert "укажите URL" in msg

    def test_non_ascii_key(self):
        ok, msg = settings_mod._check_redmine_access("https://red.example.com", "ключ123")
        assert not ok
        assert "недопустимые символы" in msg

    def test_success_with_user(self, monkeypatch):
        """Кэшированная проверка возвращает (True, None) при успехе."""
        monkeypatch.setattr(
            "admin.routes.settings.check_redmine_access_cached",
            lambda url, key: (True, None),
        )
        ok, msg = settings_mod._check_redmine_access("https://red.example.com", "key123")
        assert ok
        assert "успешно" in msg

    def test_http_error(self, monkeypatch):
        """Кэшированная проверка возвращает ошибку HTTP."""
        monkeypatch.setattr(
            "admin.routes.settings.check_redmine_access_cached",
            lambda url, key: (False, "Redmine: HTTP 403."),
        )
        ok, msg = settings_mod._check_redmine_access("https://red.example.com", "key123")
        assert not ok
        assert "HTTP 403" in msg

    def test_connect_error(self, monkeypatch):
        """Кэшированная проверка возвращает сообщение об ошибке сети."""
        monkeypatch.setattr(
            "admin.routes.settings.check_redmine_access_cached",
            lambda url, key: (False, "Redmine: нет ответа (URL/сеть)."),
        )
        ok, msg = settings_mod._check_redmine_access("https://red.example.com", "key123")
        assert not ok
        assert "нет ответа" in msg

    def test_strips_trailing_slash(self, monkeypatch):
        """URL с trailing slash — кэшированная проверка."""
        monkeypatch.setattr(
            "admin.routes.settings.check_redmine_access_cached",
            lambda url, key: (False, "Redmine: HTTP 404."),
        )
        ok, msg = settings_mod._check_redmine_access("https://red.example.com/", "key")
        assert not ok


# ═══════════════════════════════════════════════════════════════════════════
# _check_matrix_access
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckMatrixAccess:
    """Проверка подключения к Matrix."""

    def test_missing_params(self):
        ok, msg = settings_mod._check_matrix_access("", "", "")
        assert not ok
        assert "укажите homeserver" in msg

    def test_non_ascii_token(self):
        ok, msg = settings_mod._check_matrix_access("https://mx.example.com", "@bot:mx", "токен123")
        assert not ok
        assert "недопустимые символы" in msg

    @patch("admin.routes.settings.httpx.Client")
    def test_success_same_user(self, mock_client_cls):
        mock_versions = MagicMock()
        mock_versions.status_code = 200
        mock_whoami = MagicMock()
        mock_whoami.status_code = 200
        mock_whoami.json.return_value = {"user_id": "@bot:mx"}
        mock_client = MagicMock()
        mock_client.get.side_effect = [mock_versions, mock_whoami]
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        ok, msg = settings_mod._check_matrix_access("https://mx.example.com", "@bot:mx", "tok")
        assert ok
        assert "успешно" in msg

    @patch("admin.routes.settings.httpx.Client")
    def test_success_different_user(self, mock_client_cls):
        mock_versions = MagicMock()
        mock_versions.status_code = 200
        mock_whoami = MagicMock()
        mock_whoami.status_code = 200
        mock_whoami.json.return_value = {"user_id": "@other:mx"}
        mock_client = MagicMock()
        mock_client.get.side_effect = [mock_versions, mock_whoami]
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        ok, msg = settings_mod._check_matrix_access("https://mx.example.com", "@bot:mx", "tok")
        assert ok
        assert "@other:mx" in msg

    @patch("admin.routes.settings.httpx.Client")
    def test_versions_error(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 502
        mock_client = MagicMock()
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        ok, msg = settings_mod._check_matrix_access("https://mx.example.com", "@bot:mx", "tok")
        assert not ok
        assert "HTTP 502" in msg

    @patch("admin.routes.settings.httpx.Client")
    def test_whoami_error(self, mock_client_cls):
        mock_versions = MagicMock()
        mock_versions.status_code = 200
        mock_whoami = MagicMock()
        mock_whoami.status_code = 401
        mock_client = MagicMock()
        mock_client.get.side_effect = [mock_versions, mock_whoami]
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        ok, msg = settings_mod._check_matrix_access("https://mx.example.com", "@bot:mx", "tok")
        assert not ok
        assert "токен недействителен" in msg

    @patch("admin.routes.settings.httpx.Client")
    def test_connect_error(self, mock_client_cls):
        import httpx

        mock_client_cls.side_effect = httpx.ConnectError("fail")
        ok, msg = settings_mod._check_matrix_access("https://mx.example.com", "@bot:mx", "tok")
        assert not ok
        assert "нет ответа" in msg


# ═══════════════════════════════════════════════════════════════════════════
# _load_db_config_from_env
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadDbConfigFromEnv:
    """Чтение DB credentials из .env файла."""

    def test_no_env_file(self):
        with patch.object(settings_mod, "_ENV_FILE_PATH", Path("/nonexistent/.env")):
            config = settings_mod._load_db_config_from_env()
        assert config["postgres_user"] == "bot"
        assert config["postgres_db"] == "via"
        assert config["postgres_password"] == ""
        assert config["app_master_key"] == ""

    def test_with_env_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("POSTGRES_USER=myuser\n")
            f.write("POSTGRES_DB=mydb\n")
            f.write("POSTGRES_PASSWORD=mypass\n")
            f.write("APP_MASTER_KEY=mykey123\n")
            f.flush()
            tmppath = Path(f.name)

        try:
            with patch.object(settings_mod, "_ENV_FILE_PATH", tmppath):
                config = settings_mod._load_db_config_from_env()
            assert config["postgres_user"] == "myuser"
            assert config["postgres_db"] == "mydb"
            assert config["postgres_password"] == "mypass"
            assert config["app_master_key"] == "mykey123"
        finally:
            tmppath.unlink()

    def test_comments_ignored(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("# Это комментарий\n")
            f.write("POSTGRES_PASSWORD=secret\n")
            f.flush()
            tmppath = Path(f.name)

        try:
            with patch.object(settings_mod, "_ENV_FILE_PATH", tmppath):
                config = settings_mod._load_db_config_from_env()
            assert config["postgres_password"] == "secret"
        finally:
            tmppath.unlink()

    def test_empty_lines_ignored(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("\n\nPOSTGRES_DB=testdb\n\n")
            f.flush()
            tmppath = Path(f.name)

        try:
            with patch.object(settings_mod, "_ENV_FILE_PATH", tmppath):
                config = settings_mod._load_db_config_from_env()
            assert config["postgres_db"] == "testdb"
        finally:
            tmppath.unlink()

    def test_defaults_for_missing_keys(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("SOME_OTHER_VAR=foo\n")
            f.flush()
            tmppath = Path(f.name)

        try:
            with patch.object(settings_mod, "_ENV_FILE_PATH", tmppath):
                config = settings_mod._load_db_config_from_env()
            assert config["postgres_user"] == "bot"  # default
            assert config["postgres_db"] == "via"  # default
        finally:
            tmppath.unlink()


# ═══════════════════════════════════════════════════════════════════════════
# _update_env_file
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdateEnvFile:
    """Обновление переменных в .env файле."""

    def test_update_existing(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("POSTGRES_PASSWORD=old\nOTHER=value\n")
        settings_mod._update_env_file({"POSTGRES_PASSWORD": "new"}, env_path=env)
        content = env.read_text()
        assert "POSTGRES_PASSWORD=new" in content
        assert "OTHER=value" in content

    def test_add_new_key(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("EXISTING=yes\n")
        settings_mod._update_env_file({"NEW_KEY": "newval"}, env_path=env)
        content = env.read_text()
        assert "EXISTING=yes" in content
        assert "NEW_KEY=newval" in content

    def test_file_not_found(self):
        with pytest.raises(RuntimeError, match="not found"):
            settings_mod._update_env_file({"KEY": "val"}, env_path=Path("/nonexistent/.env"))

    def test_preserves_comments_and_blank_lines(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# Header comment\n\nPOSTGRES_PASSWORD=old\n# Footer\n")
        settings_mod._update_env_file({"POSTGRES_PASSWORD": "new"}, env_path=env)
        content = env.read_text()
        assert "# Header comment" in content
        assert "# Footer" in content
        assert "POSTGRES_PASSWORD=new" in content


# ═══════════════════════════════════════════════════════════════════════════
# _sanitize_matrix_device_id
# ═══════════════════════════════════════════════════════════════════════════


class TestSanitizeMatrixDeviceId:
    def test_empty(self):
        assert settings_mod._sanitize_matrix_device_id("") == ""

    def test_strips_disallowed_chars(self):
        assert settings_mod._sanitize_matrix_device_id("bot 1") == "bot1"

    def test_preserves_allowed(self):
        assert (
            settings_mod._sanitize_matrix_device_id("redmine_bot~e2ee.v2") == "redmine_bot~e2ee.v2"
        )

    def test_truncates(self):
        long_id = "a" * 300
        assert len(settings_mod._sanitize_matrix_device_id(long_id)) == 255
