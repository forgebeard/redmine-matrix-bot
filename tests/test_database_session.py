"""Тесты для src/database/session.py — URL helpers."""

from __future__ import annotations

import pytest

import database.session as sess


# ═══════════════════════════════════════════════════════════════════════════
# async_database_url
# ═══════════════════════════════════════════════════════════════════════════


class TestAsyncDatabaseUrl:
    """async_database_url: преобразование URL для asyncpg."""

    def test_sync_to_async(self):
        result = sess.async_database_url("postgresql://bot:pass@localhost:5432/via")
        assert result == "postgresql+asyncpg://bot:pass@localhost:5432/via"

    def test_already_async(self):
        result = sess.async_database_url("postgresql+asyncpg://bot:pass@localhost:5432/via")
        assert result == "postgresql+asyncpg://bot:pass@localhost:5432/via"

    def test_empty_string(self):
        assert sess.async_database_url("") == ""

    def test_none(self):
        assert sess.async_database_url(None) == ""

    def test_invalid_prefix_raises(self):
        with pytest.raises(ValueError, match="postgresql"):
            sess.async_database_url("mysql://user:pass@localhost/db")

    def test_sqlite_rejected(self):
        with pytest.raises(ValueError, match="postgresql"):
            sess.async_database_url("sqlite:///test.db")


# ═══════════════════════════════════════════════════════════════════════════
# sync_database_url_for_alembic
# ═══════════════════════════════════════════════════════════════════════════


class TestSyncDatabaseUrlForAlembic:
    """sync_database_url_for_alembic: преобразование URL для Alembic (psycopg)."""

    def test_sync_to_psycopg(self):
        result = sess.sync_database_url_for_alembic("postgresql://bot:pass@localhost:5432/via")
        assert result == "postgresql+psycopg://bot:pass@localhost:5432/via"

    def test_async_to_psycopg(self):
        result = sess.sync_database_url_for_alembic("postgresql+asyncpg://bot:pass@localhost:5432/via")
        assert result == "postgresql+psycopg://bot:pass@localhost:5432/via"

    def test_already_psycopg(self):
        result = sess.sync_database_url_for_alembic("postgresql+psycopg://bot:pass@localhost:5432/via")
        # Уже psycopg — не меняем (replace не сработает на "postgresql+psycopg://")
        assert result == "postgresql+psycopg://bot:pass@localhost:5432/via"

    def test_other_driver_unchanged(self):
        result = sess.sync_database_url_for_alembic("sqlite:///test.db")
        assert result == "sqlite:///test.db"
