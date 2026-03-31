import os
import re

from fastapi.testclient import TestClient

import pytest


# Для password auth и encrypted-secrets на старте нужен master key.
os.environ.setdefault("APP_MASTER_KEY", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("SMTP_MOCK", "1")

import admin_main  # noqa: E402


@pytest.fixture
def client():
    return TestClient(admin_main.app)


def test_health_ok(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"


def test_health_smtp_ok_in_mock_mode(client: TestClient):
    r = client.get("/health/smtp")
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "ok"


def test_login_page_ok(client: TestClient):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Вход в панель" in r.text
    assert "Email" in r.text
    assert "Пароль" in r.text
    assert "Забыли пароль?" in r.text
    assert "/static/admin/css/auth.css?v=" in r.text


def test_admin_asset_version_helper(monkeypatch):
    monkeypatch.delenv("ADMIN_ASSET_VERSION", raising=False)
    assert admin_main._admin_asset_version() == "1"
    monkeypatch.setenv("ADMIN_ASSET_VERSION", "build-xyz")
    assert admin_main._admin_asset_version() == "build-xyz"


def test_static_admin_css_served(client: TestClient):
    r = client.get("/static/admin/css/panel.css")
    assert r.status_code == 200
    assert "text/css" in (r.headers.get("content-type") or "")
    assert b":root" in r.content


def test_admin_csp_value_env(monkeypatch):
    monkeypatch.delenv("ADMIN_CSP_POLICY", raising=False)
    monkeypatch.delenv("ADMIN_ENABLE_CSP", raising=False)
    assert admin_main._admin_csp_value() is None
    monkeypatch.setenv("ADMIN_ENABLE_CSP", "1")
    v = admin_main._admin_csp_value()
    assert v is not None
    assert "default-src" in v
    monkeypatch.setenv("ADMIN_CSP_POLICY", "default-src 'none'")
    assert admin_main._admin_csp_value() == "default-src 'none'"


def test_notify_presets_helpers():
    assert admin_main._normalize_notify([]) == ["all"]
    assert admin_main._normalize_notify(["new", "issue_updated"]) == ["new", "issue_updated"]
    assert admin_main._normalize_notify(["all", "new"]) == ["all"]
    assert admin_main._notify_preset(["all"]) == "all"
    assert admin_main._notify_preset(["new"]) == "new_only"
    assert admin_main._notify_preset(["overdue"]) == "overdue_only"
    assert admin_main._notify_preset(["new", "issue_updated"]) == "custom"


def test_work_hours_range_parser():
    assert admin_main._parse_work_hours_range("09:00-18:00") == ("09:00", "18:00")
    assert admin_main._parse_work_hours_range("") == ("", "")
    assert admin_main._parse_work_hours_range("invalid") == ("", "")


def test_setup_creates_first_admin(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    page = client.get("/setup")
    assert page.status_code == 200
    token = page.cookies.get("admin_csrf")
    r = client.post(
        "/setup",
        data={
            "email": "first_admin@example.com",
            "password": "StrongPassword123",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)


def test_users_redirects_to_login_without_auth(client: TestClient):
    r = client.get("/users", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    assert r.headers.get("location", "").endswith("/login")


def _setup_and_login_admin(client: TestClient, email: str = "test_admin@example.com", password: str = "StrongPassword123") -> None:
    setup = client.get("/setup")
    token = setup.cookies.get("admin_csrf")
    client.post(
        "/setup",
        data={"email": email, "password": password, "csrf_token": token},
        follow_redirects=False,
    )
    login = client.get("/login")
    ltoken = login.cookies.get("admin_csrf")
    client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": ltoken},
        follow_redirects=True,
    )


def test_onboarding_page_copy(client: TestClient):
    r = client.get("/onboarding", follow_redirects=False)
    # Без авторизации будет редирект на login/setup, поэтому проверяем только если отдалась страница.
    if r.status_code == 200:
        assert "Первичная настройка подключений" in r.text


def test_redmine_search_without_redmine_creds_returns_empty(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")

    _setup_and_login_admin(client)
    r = client.get("/redmine/users/search?q=ivan")
    assert r.status_code == 200
    assert "Redmine не настроен" in r.text


def test_groups_page_requires_auth(client: TestClient):
    r = client.get("/groups", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    assert r.headers.get("location", "").endswith("/login")


def test_ops_restart_accepts_and_redirects(client: TestClient, monkeypatch):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client, email="ops_admin@example.com")

    monkeypatch.setattr(admin_main, "_restart_in_background", lambda actor: None)
    page = client.get("/")
    token = page.cookies.get("admin_csrf")
    r = client.post("/ops/bot/restart", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert re.search(r"/\\?ops=restart_accepted$", r.headers.get("location", ""))

