"""Тесты сборки DATABASE_URL из файла пароля (без секретов в репозитории)."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _clear_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_resolve_database_url_from_password_file(tmp_path, monkeypatch):
    pw = tmp_path / "pw"
    pw.write_text("p:w@rd", encoding="utf-8")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(pw))
    monkeypatch.setenv("POSTGRES_USER", "u1")
    monkeypatch.setenv("POSTGRES_DB", "db1")
    monkeypatch.setenv("POSTGRES_HOST", "dbhost")
    monkeypatch.setenv("POSTGRES_PORT", "5432")

    from database.url_resolver import resolve_database_url

    url = resolve_database_url()
    assert url.startswith("postgresql://u1:")
    assert "dbhost:5432/db1" in url
    assert "p%3Aw%40rd" in url or "%40" in url


def test_materialize_skips_when_database_url_set(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://a:b@localhost:5432/x")
    from database.url_resolver import materialize_database_url_env

    materialize_database_url_env()
    assert os.environ["DATABASE_URL"] == "postgresql://a:b@localhost:5432/x"


def test_resolve_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(tmp_path / "nope"))
    from database.url_resolver import resolve_database_url

    with pytest.raises(RuntimeError, match="не найден"):
        resolve_database_url()


def test_resolve_requires_password_or_url(monkeypatch):
    monkeypatch.delenv("DATABASE_PASSWORD_FILE", raising=False)
    from database.url_resolver import resolve_database_url

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        resolve_database_url()
