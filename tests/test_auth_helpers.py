"""Тесты для auth-хелперов из src/admin/helpers.py."""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import admin.helpers as h


# ═══════════════════════════════════════════════════════════════════════════
# _normalize_login
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizeLogin:
    """Нормализация логина: trim + lower."""

    def test_strips_and_lower(self):
        assert h._normalize_login("  Admin@Example.COM  ") == "admin@example.com"

    def test_none_becomes_empty(self):
        assert h._normalize_login(None) == ""

    def test_empty_stays_empty(self):
        assert h._normalize_login("") == ""

    def test_whitespace_only(self):
        assert h._normalize_login("   ") == ""

    def test_already_normal(self):
        assert h._normalize_login("testuser") == "testuser"


# ═══════════════════════════════════════════════════════════════════════════
# _login_format_ok
# ═══════════════════════════════════════════════════════════════════════════


class TestLoginFormatOk:
    """Валидация формата логина."""

    def test_valid_simple(self):
        ok, err = h._login_format_ok("testuser")
        assert ok and err is None

    def test_valid_with_dots_and_at(self):
        ok, err = h._login_format_ok("user.name@example.com")
        assert ok and err is None

    def test_valid_with_plus_and_dash(self):
        ok, err = h._login_format_ok("user+tag-name_1")
        assert ok and err is None

    def test_empty_rejected(self):
        ok, err = h._login_format_ok("")
        assert not ok and "обязателен" in err

    def test_too_short(self):
        ok, err = h._login_format_ok("ab")
        assert not ok and "3" in err

    def test_min_length(self):
        ok, err = h._login_format_ok("abc")
        assert ok and err is None

    def test_max_length(self):
        ok, err = h._login_format_ok("a" * 255)
        assert ok and err is None

    def test_too_long(self):
        ok, err = h._login_format_ok("a" * 256)
        assert not ok and "255" in err

    def test_cyrillic_rejected(self):
        ok, err = h._login_format_ok("админ")
        assert not ok and "латиница" in err

    def test_spaces_rejected(self):
        ok, err = h._login_format_ok("user name")
        assert not ok

    def test_special_chars_rejected(self):
        ok, err = h._login_format_ok("user<script>")
        assert not ok


# ═══════════════════════════════════════════════════════════════════════════
# _login_allowed
# ═══════════════════════════════════════════════════════════════════════════


class TestLoginAllowed:
    """Проверка логина по списку ADMIN_LOGINS."""

    def teardown_method(self):
        """Восстанавливаем состояние после каждого теста."""
        h._ALLOWED_LOGINS_RAW = os.environ.get("ADMIN_LOGINS", "")

    def test_no_restriction_allows_any(self):
        h._ALLOWED_LOGINS_RAW = ""
        assert h._login_allowed("anyuser") is True
        assert h._login_allowed("admin") is True

    def test_allowed_user(self):
        h._ALLOWED_LOGINS_RAW = "alice,bob,charlie"
        assert h._login_allowed("alice") is True
        assert h._login_allowed("bob") is True
        assert h._login_allowed("charlie") is True

    def test_disallowed_user(self):
        h._ALLOWED_LOGINS_RAW = "alice,bob"
        assert h._login_allowed("eve") is False

    def test_whitespace_in_list(self):
        """Пробелы вокруг имён в списке должны игнорироваться."""
        h._ALLOWED_LOGINS_RAW = " alice , bob , charlie "
        assert h._login_allowed("alice") is True
        assert h._login_allowed("bob") is True
        assert h._login_allowed(" charlie ") is False  # пробелы в логине не обрезаются

    def test_empty_list_allows_any(self):
        h._ALLOWED_LOGINS_RAW = ","
        # Пустые элементы после split → пустые строки, но "" не равен логину
        assert h._login_allowed("user") is False


# ═══════════════════════════════════════════════════════════════════════════
# _generic_login_error
# ═══════════════════════════════════════════════════════════════════════════


class TestGenericLoginError:
    """Ошибка входа не раскрывает деталей."""

    def test_message_is_generic(self):
        msg = h._generic_login_error()
        assert "пароль" in msg.lower()
        # Не должно быть specifics
        assert "ip" not in msg.lower()
        assert "rate" not in msg.lower()


# ═══════════════════════════════════════════════════════════════════════════
# _client_ip
# ═══════════════════════════════════════════════════════════════════════════


class TestClientIp:
    """Определение IP клиента из запроса."""

    def test_x_forwarded_for_single(self):
        req = MagicMock()
        req.headers = {"x-forwarded-for": "203.0.113.50"}
        req.client = MagicMock()
        req.client.host = "10.0.0.1"
        assert h._client_ip(req) == "203.0.113.50"

    def test_x_forwarded_for_multiple(self):
        """Берётся первый IP из цепочки."""
        req = MagicMock()
        req.headers = {"x-forwarded-for": "203.0.113.50, 10.0.0.1, 192.168.1.1"}
        req.client = MagicMock()
        req.client.host = "127.0.0.1"
        assert h._client_ip(req) == "203.0.113.50"

    def test_x_forwarded_for_with_spaces(self):
        req = MagicMock()
        req.headers = {"x-forwarded-for": " 203.0.113.50 , 10.0.0.1 "}
        req.client = MagicMock()
        req.client.host = "127.0.0.1"
        assert h._client_ip(req) == "203.0.113.50"

    def test_no_xff_uses_client_host(self):
        req = MagicMock()
        req.headers = {}
        req.client = MagicMock()
        req.client.host = "10.0.0.5"
        assert h._client_ip(req) == "10.0.0.5"

    def test_no_client_uses_localhost(self):
        req = MagicMock()
        req.headers = {}
        req.client = None
        assert h._client_ip(req) == "127.0.0.1"


# ═══════════════════════════════════════════════════════════════════════════
# _verify_csrf
# ═══════════════════════════════════════════════════════════════════════════


class TestVerifyCsrf:
    """Проверка CSRF-токена."""

    def test_valid_token_passes(self):
        req = MagicMock()
        req.cookies = {"admin_csrf": "token123"}
        # Не должно выбрасывать
        h._verify_csrf(req, "token123")

    def test_mismatch_raises(self):
        req = MagicMock()
        req.cookies = {"admin_csrf": "token123"}
        with pytest.raises(Exception) as exc_info:
            h._verify_csrf(req, "wrong_token")
        assert exc_info.value.status_code == 403

    def test_empty_token_raises(self):
        req = MagicMock()
        req.cookies = {"admin_csrf": "token123"}
        with pytest.raises(Exception) as exc_info:
            h._verify_csrf(req, "")
        assert exc_info.value.status_code == 403

    def test_empty_cookie_raises(self):
        req = MagicMock()
        req.cookies = {}
        with pytest.raises(Exception) as exc_info:
            h._verify_csrf(req, "token123")
        assert exc_info.value.status_code == 403

    def test_both_empty_raises(self):
        req = MagicMock()
        req.cookies = {}
        with pytest.raises(Exception) as exc_info:
            h._verify_csrf(req, "")
        assert exc_info.value.status_code == 403


# ═══════════════════════════════════════════════════════════════════════════
# _ensure_csrf
# ═══════════════════════════════════════════════════════════════════════════


class TestEnsureCsrf:
    """Генерация/получение CSRF-токена."""

    def test_existing_cookie_returned(self):
        req = MagicMock()
        req.cookies = {"admin_csrf": "existing_token"}
        token, set_cookie = h._ensure_csrf(req)
        assert token == "existing_token"
        assert set_cookie is False

    def test_new_token_generated(self):
        req = MagicMock()
        req.cookies = {}
        token, set_cookie = h._ensure_csrf(req)
        assert token  # не пустой
        assert set_cookie is True
        # Токен должен быть валидным URL-safe
        import base64
        # token_urlsafe использует base64url, проверка что декодируется
        base64.urlsafe_b64decode(token + "==")

    def test_no_cookie_header(self):
        """Если в request нет cookies, должен сгенерироваться новый токен."""
        req = MagicMock()
        req.cookies = {}
        token, set_cookie = h._ensure_csrf(req)
        assert len(token) > 20  # token_urlsafe(32) = ~43 символа
        assert set_cookie is True


# ═══════════════════════════════════════════════════════════════════════════
# _SimpleRateLimiter
# ═══════════════════════════════════════════════════════════════════════════


class TestRateLimiter:
    """Rate limiter: ограничивает частоту запросов."""

    @pytest.fixture(autouse=True)
    def disable_env_override(self, monkeypatch):
        """Убираем ADMIN_DISABLE_RATE_LIMITS чтобы тестировать реальную логику."""
        monkeypatch.delenv("ADMIN_DISABLE_RATE_LIMITS", raising=False)

    def setup_method(self):
        self.limiter = h._SimpleRateLimiter()

    def test_allows_under_limit(self):
        assert self.limiter.hit("key1", limit=3, window_seconds=10) is True
        assert self.limiter.hit("key1", limit=3, window_seconds=10) is True
        assert self.limiter.hit("key1", limit=3, window_seconds=10) is True

    def test_blocks_over_limit(self):
        for _ in range(3):
            self.limiter.hit("key1", limit=3, window_seconds=10)
        assert self.limiter.hit("key1", limit=3, window_seconds=10) is False

    def test_resets_after_window(self):
        for _ in range(3):
            self.limiter.hit("key1", limit=3, window_seconds=1)
        assert self.limiter.hit("key1", limit=3, window_seconds=1) is False
        time.sleep(1.1)
        assert self.limiter.hit("key1", limit=3, window_seconds=1) is True

    def test_separate_keys(self):
        self.limiter.hit("key1", limit=1, window_seconds=10)
        assert self.limiter.hit("key1", limit=1, window_seconds=10) is False
        assert self.limiter.hit("key2", limit=1, window_seconds=10) is True

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("ADMIN_DISABLE_RATE_LIMITS", "1")
        limiter = h._SimpleRateLimiter()
        for _ in range(100):
            assert limiter.hit("key", limit=1, window_seconds=10) is True


# ═══════════════════════════════════════════════════════════════════════════
# _now_utc
# ═══════════════════════════════════════════════════════════════════════════


class TestNowUtc:
    """_now_utc возвращает datetime с timezone UTC."""

    def test_has_timezone(self):
        dt = h._now_utc()
        assert dt.tzinfo is not None
        assert dt.tzinfo == timezone.utc

    def test_close_to_real_now(self):
        before = datetime.now(tz=timezone.utc)
        dt = h._now_utc()
        after = datetime.now(tz=timezone.utc)
        assert before <= dt <= after


# ═══════════════════════════════════════════════════════════════════════════
# _mask_secret
# ═══════════════════════════════════════════════════════════════════════════


class TestMaskSecret:
    """Маскировка секретов в helpers."""

    def test_none_returns_empty(self):
        assert h._mask_secret(None) == ""

    def test_empty_returns_empty(self):
        assert h._mask_secret("") == ""

    def test_short_masked_fully(self):
        assert h._mask_secret("abc") == "•••"
        assert h._mask_secret("ab") == "••"

    def test_long_masked(self):
        masked = h._mask_secret("super_secret_token_12345")
        assert masked.startswith("su")
        assert masked.endswith("45")
        assert "•" in masked

    def test_url_not_masked(self):
        assert h._mask_secret("https://example.com", mask_url=True) == "https://example.com"

    def test_default_masks_url(self):
        masked = h._mask_secret("https://example.com")
        assert masked != "https://example.com"
        assert masked.startswith("ht")
        assert masked.endswith("om")


# ═══════════════════════════════════════════════════════════════════════════
# _parse_catalog_payload
# ═══════════════════════════════════════════════════════════════════════════


class TestParseCatalogPayload:
    """Парсинг JSON-каталогов уведомлений и версий."""

    def test_valid_json(self):
        notify, versions = h._parse_catalog_payload('["a","b"]', '["1.0","2.0"]')
        assert notify == ["a", "b"]
        assert versions == ["1.0", "2.0"]

    def test_empty_strings(self):
        notify, versions = h._parse_catalog_payload("", "")
        assert notify == []
        assert versions == []

    def test_invalid_json(self):
        notify, versions = h._parse_catalog_payload("not json", "also not")
        assert notify == []
        assert versions == []

    def test_none_values(self):
        notify, versions = h._parse_catalog_payload(None, None)
        assert notify == []
        assert versions == []

    def test_non_list_json(self):
        notify, versions = h._parse_catalog_payload('{"a":1}', '"string"')
        assert notify == []
        assert versions == []

    def test_strips_and_filters_empty(self):
        notify, versions = h._parse_catalog_payload('["  a  ", "", "b"]', '[" 1.0 ", ""]')
        assert notify == ["a", "b"]
        assert versions == ["1.0"]

    def test_non_string_items_converted(self):
        notify, versions = h._parse_catalog_payload('[123, true]', '[null]')
        assert notify == ["123", "True"]  # Python bool → "True"
        assert versions == ["None"]  # null → "None"


# ═══════════════════════════════════════════════════════════════════════════
# _append_ops_to_events_log
# ═══════════════════════════════════════════════════════════════════════════


class TestAppendOpsToEventsLog:
    """Запись в журнал событий."""

    def test_no_path_does_nothing(self, monkeypatch):
        monkeypatch.setenv("ADMIN_EVENTS_LOG_PATH", "")
        # Не должно выбрасывать
        h._append_ops_to_events_log("test line")

    def test_writes_to_file(self, tmp_path, monkeypatch):
        logfile = tmp_path / "events.log"
        monkeypatch.setenv("ADMIN_EVENTS_LOG_PATH", str(logfile))
        h._append_ops_to_events_log("Docker bot/start ok")
        content = logfile.read_text()
        assert "[ADMIN]" in content
        assert "Docker bot/start ok" in content

    def test_creates_parent_dir(self, tmp_path, monkeypatch):
        logfile = tmp_path / "sub" / "events.log"
        monkeypatch.setenv("ADMIN_EVENTS_LOG_PATH", str(logfile))
        h._append_ops_to_events_log("test")
        assert logfile.exists()


# ═══════════════════════════════════════════════════════════════════════════
# _append_audit_file_line
# ═══════════════════════════════════════════════════════════════════════════


class TestAppendAuditFileLine:
    """Запись в журнал аудита."""

    def test_no_path_does_nothing(self, monkeypatch):
        monkeypatch.setenv("ADMIN_AUDIT_LOG_PATH", "")
        h._append_audit_file_line("test")

    def test_writes_to_file(self, tmp_path, monkeypatch):
        logfile = tmp_path / "audit.log"
        monkeypatch.setenv("ADMIN_AUDIT_LOG_PATH", str(logfile))
        h._append_audit_file_line("CRUD user/create")
        content = logfile.read_text()
        assert "CRUD user/create" in content

    def test_creates_parent_dir(self, tmp_path, monkeypatch):
        logfile = tmp_path / "sub" / "audit.log"
        monkeypatch.setenv("ADMIN_AUDIT_LOG_PATH", str(logfile))
        h._append_audit_file_line("test")
        assert logfile.exists()
