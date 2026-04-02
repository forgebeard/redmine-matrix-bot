import asyncio
import os
import re

from fastapi.testclient import TestClient

import pytest
from sqlalchemy import select


# Для password auth и encrypted-secrets на старте нужен master key.
os.environ.setdefault("APP_MASTER_KEY", "0123456789abcdef0123456789abcdef")
# Тесты /setup и /login не должны зависеть от локального ADMIN_LOGINS в окружении разработчика.
os.environ.pop("ADMIN_LOGINS", None)

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


def test_login_page_ok(client: TestClient):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Вход в панель" in r.text
    assert "Логин" in r.text
    assert "Пароль" in r.text
    assert "Войти" in r.text
    assert "/static/admin/css/auth.css?v=" in r.text


def test_forgot_password_redirects_to_login(client: TestClient):
    r = client.get("/forgot-password", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("location", "").endswith("/login")
    p = client.post("/forgot-password", data={"login": "x"}, follow_redirects=False)
    assert p.status_code == 303
    assert p.headers.get("location", "").endswith("/login")


def test_admin_asset_version_helper(monkeypatch):
    monkeypatch.delenv("ADMIN_ASSET_VERSION", raising=False)
    assert admin_main._admin_asset_version() == "4"
    monkeypatch.setenv("ADMIN_ASSET_VERSION", "build-xyz")
    assert admin_main._admin_asset_version() == "build-xyz"


def test_static_admin_css_served(client: TestClient):
    r = client.get("/static/admin/css/panel.css")
    assert r.status_code == 200
    assert "text/css" in (r.headers.get("content-type") or "")
    assert b":root" in r.content


def test_ops_flash_includes_docker_detail():
    msg = admin_main._ops_flash_message("stop_error", "Docker API HTTP 403: denied")
    assert msg is not None
    assert "403" in msg
    assert "denied" in msg


def test_append_ops_to_events_log(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_main, "_admin_events_log_path", lambda: tmp_path / "ev.log")
    admin_main._append_ops_to_events_log("Docker bot/stop ok")
    text = (tmp_path / "ev.log").read_text(encoding="utf-8")
    assert "[ADMIN]" in text
    assert "Docker bot/stop ok" in text
    first = text.strip().splitlines()[0]
    assert re.match(r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2} \[ADMIN\]", first)
    assert "," not in first[:25]  # без миллисекунд в префиксе времени


def test_dash_service_strip_redirects_without_auth(client: TestClient):
    r = client.get("/dash/service-strip", follow_redirects=False)
    assert r.status_code in (301, 302, 303)


def test_app_users_reset_password_policy_error_is_html_not_json(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    r = client.get("/app-users")
    if r.status_code != 200:
        pytest.skip("Нет доступа к /app-users")
    m = re.search(r'action="/app-users/([a-f0-9-]{36})/reset-password-admin"', r.text)
    assert m, "ожидали форму сброса пароля с UUID"
    uid = m.group(1)
    token = client.cookies.get("admin_csrf", "")
    pr = client.post(
        f"/app-users/{uid}/reset-password-admin",
        data={"new_password": "short", "csrf_token": token},
        follow_redirects=False,
    )
    assert pr.status_code == 200
    ct = (pr.headers.get("content-type") or "").lower()
    assert "text/html" in ct
    assert "application/json" not in ct
    assert "Пароль должен содержать минимум 12 символов" in pr.text


def test_dash_service_strip_for_admin(client: TestClient, monkeypatch):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    monkeypatch.setattr(
        admin_main,
        "get_service_status",
        lambda: {
            "service": "bot",
            "state": "running",
            "running": True,
            "container_id": "deadbeef",
            "container_name": "proj-bot-1",
            "docker_status": "running",
            "started_at": "2026-01-01T12:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        admin_main,
        "_runtime_status_from_file",
        lambda: {"last_cycle_at": "2026-01-01T00:00:00Z", "last_cycle_duration_s": 1.2, "error_count": 0},
    )
    r = client.get("/dash/service-strip")
    assert r.status_code == 200
    assert "Статус:" in r.text
    assert "Включен" in r.text
    assert "proj-bot-1" not in r.text
    assert "Uptime" in r.text


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


def test_a_setup_creates_first_admin(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    page = client.get("/setup", follow_redirects=False)
    if page.status_code != 200:
        pytest.skip("Форма /setup недоступна (админ уже создан — типично при повторном pytest на той же БД)")
    token = client.cookies.get("admin_csrf")
    r = client.post(
        "/setup",
        data={
            "login": "test_admin@example.com",
            "password": "StrongPassword123",
            "password_confirm": "StrongPassword123",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)


def test_users_redirects_to_login_without_auth(client: TestClient):
    r = client.get("/users", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    loc = r.headers.get("location", "")
    # Пустая БД: первый шаг — /setup; если админ уже есть — /login.
    assert loc.endswith("/login") or loc.endswith("/setup"), loc


def _setup_and_login_admin(client: TestClient, login: str = "test_admin@example.com", password: str = "StrongPassword123") -> None:
    client.get("/setup", follow_redirects=True)
    token = client.cookies.get("admin_csrf")
    created = client.post(
        "/setup",
        data={
            "login": login,
            "password": password,
            "password_confirm": password,
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    # На одной БД несколько тестов: первый создаёт админа, остальные получают 409.
    assert created.status_code in (302, 303, 409), created.status_code
    client.get("/login")
    ltoken = client.cookies.get("admin_csrf")
    logged = client.post(
        "/login",
        data={"login": login, "password": password, "csrf_token": ltoken},
        follow_redirects=False,
    )
    if logged.status_code == 401:
        pytest.skip(
            "Вход тестового admin не удался (в БД другой пароль или нет пользователя). "
            "Используйте чистую БД или задайте учётные данные под вашу БД."
        )
    assert logged.status_code in (302, 303), logged.status_code


def test_onboarding_page_copy(client: TestClient):
    r = client.get("/onboarding", follow_redirects=False)
    # Без авторизации будет редирект на login/setup, поэтому проверяем только если отдалась страница.
    if r.status_code == 200:
        assert "Настройки подключений" in r.text


def test_parse_status_keys_list_dedup_and_order():
    assert admin_main._parse_status_keys_list("a, b\na, c") == ["a", "b", "c"]
    assert admin_main._parse_status_keys_list("") == []


def test_groups_assignable_excludes_filter_all_label():
    class _G:
        def __init__(self, name: str):
            self.name = name

    rows = [
        _G(admin_main.GROUP_UNASSIGNED_NAME),
        _G(admin_main.GROUP_USERS_FILTER_ALL_LABEL),
        _G("Линия поддержки"),
    ]
    out = admin_main._groups_assignable(rows)
    assert [g.name for g in out] == ["Линия поддержки"]


def test_groups_assignable_excludes_filter_label_case_insensitive():
    class _G:
        def __init__(self, name: str):
            self.name = name

    rows = [
        _G("ВСЕ ГРУППЫ"),
        _G("все группы"),
        _G("Линия"),
    ]
    out = admin_main._groups_assignable(rows)
    assert [g.name for g in out] == ["Линия"]


def test_redmine_lookup_requires_auth(client: TestClient):
    r = client.get("/redmine/users/lookup?user_id=1", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    loc = r.headers.get("location", "")
    assert loc.endswith("/login") or loc.endswith("/setup"), loc


def test_redmine_lookup_not_configured_json(client: TestClient, monkeypatch):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    monkeypatch.setattr(admin_main, "REDMINE_URL", "")
    monkeypatch.setattr(admin_main, "REDMINE_API_KEY", "")
    _setup_and_login_admin(client)
    r = client.get("/redmine/users/lookup?user_id=1")
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "not_configured"}


def test_redmine_search_without_redmine_creds_returns_empty(client: TestClient, monkeypatch):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")

    monkeypatch.setattr(admin_main, "REDMINE_URL", "")
    monkeypatch.setattr(admin_main, "REDMINE_API_KEY", "")
    _setup_and_login_admin(client)
    r = client.get("/redmine/users/search?q=ivan")
    assert r.status_code == 200
    assert "Redmine не настроен" in r.text  # фрагмент «…(нет URL/API key)»


def test_groups_page_requires_auth(client: TestClient):
    r = client.get("/groups", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    loc = r.headers.get("location", "")
    assert loc.endswith("/login") or loc.endswith("/setup"), loc


def test_events_page_requires_auth(client: TestClient):
    r = client.get("/events", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    loc = r.headers.get("location", "")
    assert loc.endswith("/login") or loc.endswith("/setup"), loc


def test_read_log_tail_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_EVENTS_LOG_PATH", str(tmp_path / "missing.log"))
    text = admin_main._read_log_tail(admin_main._admin_events_log_path())
    assert "не найден" in text


def test_read_log_tail_keeps_last_lines(monkeypatch, tmp_path):
    logf = tmp_path / "bot.log"
    monkeypatch.setenv("ADMIN_EVENTS_LOG_PATH", str(logf))
    logf.write_text("\n".join(f"L{i:04d}" for i in range(500)), encoding="utf-8")
    out = admin_main._read_log_tail(admin_main._admin_events_log_path(), max_lines=12)
    assert "L0499" in out
    assert "L0000" not in out


def test_dash_events_tail_line_count(monkeypatch, tmp_path):
    monkeypatch.setenv("ADMIN_EVENTS_LOG_PATH", str(tmp_path / "none.log"))
    assert admin_main._dash_events_tail_line_count() == 0
    logf = tmp_path / "bot.log"
    monkeypatch.setenv("ADMIN_EVENTS_LOG_PATH", str(logf))
    logf.write_text("a\n\nb\nc\n", encoding="utf-8")
    assert admin_main._dash_events_tail_line_count(max_lines=400) == 3


def test_me_settings_admin_redirects_home(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    r = client.get("/me/settings", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers.get("location", "")
    assert loc in ("http://testserver/", "/")


def test_groups_create_reserved_name_rejected(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    token = client.cookies.get("admin_csrf")
    r = client.post(
        "/groups",
        data={
            "name": admin_main.GROUP_UNASSIGNED_NAME,
            "room_id": "",
            "timezone_name": "",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_groups_create_requires_room_and_statuses(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    token = client.cookies.get("admin_csrf")
    base = {
        "name": "pytest_tmp_group_validation",
        "timezone_name": "",
        "is_active": "1",
        "notify_preset": "all",
        "csrf_token": token,
    }
    r1 = client.post(
        "/groups",
        data={**base, "room_id": "", "initial_status_keys": "Новая"},
        follow_redirects=False,
    )
    assert r1.status_code == 400
    r2 = client.post(
        "/groups",
        data={**base, "room_id": "!pytest:matrix", "initial_status_keys": ""},
        follow_redirects=False,
    )
    assert r2.status_code == 400


def test_groups_delete_unassigned_forbidden(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")

    from database.models import SupportGroup
    from database.session import get_session_factory

    async def _unassigned_id() -> int | None:
        factory = get_session_factory()
        async with factory() as session:
            res = await session.execute(
                select(SupportGroup.id).where(SupportGroup.name == admin_main.GROUP_UNASSIGNED_NAME)
            )
            return res.scalar_one_or_none()

    gid = asyncio.run(_unassigned_id())
    if gid is None:
        pytest.skip("В БД нет системной группы UNASSIGNED (нужны миграции)")
    _setup_and_login_admin(client)
    token = client.cookies.get("admin_csrf")
    r = client.post(f"/groups/{gid}/delete", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 403


def test_events_tail_ok_when_authed(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    r = client.get("/events/tail", follow_redirects=False)
    assert r.status_code == 200
    assert "<pre " in r.text


def test_ops_restart_accepts_and_redirects(client: TestClient, monkeypatch):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)

    monkeypatch.setattr(admin_main, "_restart_in_background", lambda actor: None)
    page = client.get("/")
    token = client.cookies.get("admin_csrf")
    r = client.post("/ops/bot/restart", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers.get("location") == "/dashboard?ops=restart_accepted"

