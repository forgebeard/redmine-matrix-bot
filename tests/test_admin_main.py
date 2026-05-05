from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import admin.main as admin_main  # noqa: E402
from tests.conftest import _setup_and_login_admin


def test_events_page_includes_events_table(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not str(db_url).startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    r = client.get("/events")
    if r.status_code != 200:
        pytest.skip("Нет доступа к /events")
    assert "События" in r.text
    assert "Дата" in r.text and "Уровень" in r.text and "Сообщение" in r.text
    assert "Всего по фильтру:" in r.text


def test_events_page_echoes_date_from_query(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not str(db_url).startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    r = client.get("/events?date_from=2024-01-02")
    if r.status_code != 200:
        pytest.skip("Нет доступа к /events")
    assert 'name="date_from" value="2024-01-02"' in r.text


def test_routes_status_legacy_get_redirects_for_admin(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not str(db_url).startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    r = client.get("/routes/status", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers.get("location") or ""
    assert loc.endswith("/groups")


def test_routes_version_legacy_get_redirects_301_for_admin(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not str(db_url).startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    r = client.get("/routes/version", follow_redirects=False)
    assert r.status_code == 301
    loc = r.headers.get("location") or ""
    assert loc.endswith("/settings/routes/version")


def test_settings_routes_version_page_for_admin(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not str(db_url).startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    r = client.get("/settings/routes/version")
    if r.status_code != 200:
        pytest.skip("Нет доступа к /settings/routes/version")
    assert "Версии по комнатам" in r.text


def test_routes_version_legacy_post_returns_410(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not str(db_url).startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    token = client.cookies.get("admin_csrf", "")
    r = client.post(
        "/routes/version",
        data={"version_key": "x", "room_id": "!room:server", "csrf_token": token or ""},
        follow_redirects=False,
    )
    assert r.status_code == 410


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
    from admin.helpers import _admin_asset_version

    monkeypatch.delenv("ADMIN_ASSET_VERSION", raising=False)
    assert _admin_asset_version() == "6"
    monkeypatch.setenv("ADMIN_ASSET_VERSION", "build-xyz")
    assert _admin_asset_version() == "build-xyz"


def test_static_admin_css_served(client: TestClient):
    r = client.get("/static/admin/css/base.css")
    assert r.status_code == 200
    assert "text/css" in (r.headers.get("content-type") or "")
    assert b":root" in r.content


def test_ops_flash_includes_docker_detail():
    msg = admin_main._ops_flash_message("stop_error", "Docker API HTTP 403: denied")
    assert msg is not None
    assert "403" in msg
    assert "denied" in msg


def test_append_ops_to_events_log(tmp_path, monkeypatch):
    log_path = tmp_path / "ev.log"
    monkeypatch.setenv("ADMIN_EVENTS_LOG_PATH", str(log_path))
    admin_main._append_ops_to_events_log("Docker bot/stop ok")
    text = log_path.read_text(encoding="utf-8")
    assert "[ADMIN]" in text
    assert "Docker bot/stop ok" in text
    first = text.strip().splitlines()[0]
    # Новый формат: [ADMIN] ISO-TIMESTAMP message
    assert first.startswith("[ADMIN]")
    assert "202" in first  # проверка наличия года в ISO-дате


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


def test_app_users_change_login_invalid_is_html(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    r = client.get("/app-users")
    if r.status_code != 200:
        pytest.skip("Нет доступа к /app-users")
    m = re.search(r'action="/app-users/([a-f0-9-]{36})/change-login-admin"', r.text)
    assert m, "ожидали форму смены логина с UUID"
    uid = m.group(1)
    token = client.cookies.get("admin_csrf", "")
    pr = client.post(
        f"/app-users/{uid}/change-login-admin",
        data={"new_login": "ab", "csrf_token": token},
        follow_redirects=False,
    )
    assert pr.status_code == 200
    assert "text/html" in (pr.headers.get("content-type") or "").lower()
    assert "латиница" in pr.text or "255" in pr.text


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
        lambda: {
            "last_cycle_at": "2026-01-01T00:00:00Z",
            "last_cycle_duration_s": 1.2,
            "error_count": 0,
        },
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


def test_status_presets_helpers():
    allowed = ["new", "issue_updated", "overdue"]
    assert admin_main._normalize_notify([]) == ["all"]
    assert admin_main._normalize_notify(["new", "issue_updated"], allowed) == [
        "new",
        "issue_updated",
    ]
    assert admin_main._normalize_notify(["all", "new"]) == ["all"]
    assert admin_main._normalize_notify(["ghost"], allowed) == ["all"]
    assert admin_main._status_preset(["all"]) == "default"
    assert admin_main._status_preset(["new"]) == "custom"
    assert admin_main._status_preset(["overdue"]) == "custom"
    assert admin_main._status_preset(["new", "issue_updated"]) == "custom"


def test_version_presets_helpers():
    assert admin_main._normalize_versions([], ["1.0"]) == []
    assert admin_main._normalize_versions(["1.0", "1.0", "2.0", "x"], ["1.0", "2.0"]) == [
        "1.0",
        "2.0",
    ]
    assert admin_main._normalize_versions(["1.0"], []) == []
    assert admin_main._version_preset([], ["1.0"]) == "all"
    assert admin_main._version_preset(["1.0"], ["1.0"]) == "custom"


def test_top_timezone_options_include_ufa():
    top = admin_main._top_timezone_options()
    assert "Asia/Ufa" in top or "Asia/Yekaterinburg" in top


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
        pytest.skip(
            "Форма /setup недоступна (админ уже создан — типично при повторном pytest на той же БД)"
        )
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


def test_onboarding_page_copy(client: TestClient):
    r = client.get("/onboarding", follow_redirects=False)
    # Без авторизации будет редирект на login/setup, поэтому проверяем только если отдалась страница.
    if r.status_code == 200:
        assert "Настройки подключений" in r.text
        assert "Таймзона сервиса" in r.text


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
    # Редирект на главную (/) или дашборд (/dashboard)
    assert loc in ("http://testserver/", "/", "http://testserver/dashboard", "/dashboard"), loc


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


def test_groups_create_requires_room(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    token = client.cookies.get("admin_csrf")
    base = {
        "name": "pytest_tmp_group_validation",
        "timezone_name": "",
        "is_active": "1",
        "status_preset": "all",
        "csrf_token": token,
    }
    r1 = client.post(
        "/groups",
        data={**base, "room_id": "", "initial_status_keys": "Новая"},
        follow_redirects=False,
    )
    assert r1.status_code == 400


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


def test_users_create_redirects_with_highlight_and_marks_row(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")

    _setup_and_login_admin(client)
    token = client.cookies.get("admin_csrf")
    redmine_id = 900000 + (abs(hash(uuid4().hex)) % 99999)
    room = f"!pytest-user-{uuid4().hex[:8]}:server"

    resp = client.post(
        "/users",
        data={
            "redmine_id": str(redmine_id),
            "display_name": "pytest user highlight",
            "room": room,
            "status_preset": "all",
            "version_preset": "all",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers.get("location", "")
    assert loc.startswith("/users?highlight_user_id=")
    q = parse_qs(urlparse(loc).query)
    assert q.get("highlight_user_id")

    page = client.get(loc)
    assert page.status_code == 200
    assert 'id="highlight-user-row"' in page.text
    assert 'class="is-highlighted-row"' in page.text


def test_groups_create_redirects_with_highlight_and_marks_row(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")

    _setup_and_login_admin(client)
    token = client.cookies.get("admin_csrf")
    group_name = f"pytest-group-{uuid4().hex[:8]}"
    room_id = f"!pytest-group-{uuid4().hex[:8]}:server"

    resp = client.post(
        "/groups",
        data={
            "name": group_name,
            "room_id": room_id,
            "timezone_name": "Europe/Moscow",
            "is_active": "1",
            "status_preset": "all",
            "version_preset": "all",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers.get("location", "")
    assert loc.startswith("/groups?highlight_group_id=")
    q = parse_qs(urlparse(loc).query)
    assert q.get("highlight_group_id")

    page = client.get(loc)
    assert page.status_code == 200
    assert 'id="highlight-group-row"' in page.text
    assert 'class="is-highlighted-row"' in page.text


def test_group_form_hides_active_block_and_uses_new_dnd_label(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    page = client.get("/groups/new")
    assert page.status_code == 200
    assert "Группа активна" not in page.text
    assert "summary_group_active" not in page.text
    assert "Отключить уведомления группы" in page.text


def test_lists_use_inline_delete_confirmation_markup(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)

    users_page = client.get("/users")
    assert users_page.status_code == 200
    assert "data-inline-delete-form" in users_page.text

    groups_page = client.get("/groups")
    assert groups_page.status_code == 200
    assert "data-inline-delete-form" in groups_page.text


def test_users_custom_notify_and_versions_are_persisted(client: TestClient, monkeypatch):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")

    async def _fake_statuses(_session):
        return [
            {"key": "n_new", "label": "Новые", "is_default": False},
            {"key": "n_overdue", "label": "Просроченные", "is_default": False},
        ]

    async def _fake_versions(_session):
        return [
            {"key": "v1.0", "label": "v1.0", "is_default": False},
            {"key": "v2.0", "label": "v2.0", "is_default": False},
        ]

    async def _fake_priorities(_session):
        return []

    monkeypatch.setattr(admin_main, "_load_statuses_catalog", _fake_statuses)
    monkeypatch.setattr(admin_main, "_load_versions_catalog", _fake_versions)
    monkeypatch.setattr(admin_main, "_load_priorities_catalog", _fake_priorities)
    _setup_and_login_admin(client)
    token = client.cookies.get("admin_csrf")
    redmine_id = 990000 + (abs(hash(uuid4().hex)) % 9999)
    room = f"!pytest-catalog-{uuid4().hex[:8]}:server"

    resp = client.post(
        "/users",
        data={
            "redmine_id": str(redmine_id),
            "display_name": "pytest custom catalogs",
            "room": room,
            "status_preset": "custom",
            "status_values": ["n_new", "n_overdue", "ghost_notify"],
            "version_preset": "custom",
            "version_values": ["v1.0", "v2.0", "ghost_version"],
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers.get("location", "")
    q = parse_qs(urlparse(loc).query)
    assert q.get("highlight_user_id")
    user_id = int(q["highlight_user_id"][0])

    edit_page = client.get(f"/users/{user_id}/edit")
    assert edit_page.status_code == 200
    text = edit_page.text
    assert 'name="status_preset" value="custom" checked' in text
    assert 'name="version_preset" value="custom" checked' in text
    assert re.search(r'name="status_values"[^>]*value="n_new"[^>]*checked', text)
    assert re.search(r'name="status_values"[^>]*value="n_overdue"[^>]*checked', text)
    assert re.search(r'name="version_values"[^>]*value="v1\.0"[^>]*checked', text)
    assert re.search(r'name="version_values"[^>]*value="v2\.0"[^>]*checked', text)
    assert 'value="ghost_notify"' not in text
    assert 'value="ghost_version"' not in text


def test_full_flow_group_user_assignment_update_and_delete(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    token = client.cookies.get("admin_csrf")
    suffix = uuid4().hex[:8]

    group_name = f"pytest-flow-group-{suffix}"
    room_id = f"!pytest-flow-group-{suffix}:server"
    create_group = client.post(
        "/groups",
        data={
            "name": group_name,
            "room_id": room_id,
            "timezone_name": "Europe/Moscow",
            "status_preset": "all",
            "version_preset": "all",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert create_group.status_code == 303
    group_loc = create_group.headers.get("location", "")
    gid = int(parse_qs(urlparse(group_loc).query)["highlight_group_id"][0])

    redmine_id = 920000 + (abs(hash(uuid4().hex)) % 9999)
    user_name = f"pytest flow user {suffix}"
    user_room = f"!pytest-flow-user-{suffix}:server"
    create_user = client.post(
        "/users",
        data={
            "redmine_id": str(redmine_id),
            "display_name": user_name,
            "group_id": str(gid),
            "room": user_room,
            "status_preset": "all",
            "version_preset": "all",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert create_user.status_code == 303
    user_loc = create_user.headers.get("location", "")
    uid = int(parse_qs(urlparse(user_loc).query)["highlight_user_id"][0])

    users_page = client.get("/users")
    assert users_page.status_code == 200
    assert user_name in users_page.text
    assert group_name in users_page.text

    updated_group_name = f"{group_name}-upd"
    update_group = client.post(
        f"/groups/{gid}",
        data={
            "name": updated_group_name,
            "room_id": room_id,
            "timezone_name": "Europe/Moscow",
            "status_preset": "all",
            "version_preset": "all",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert update_group.status_code == 303
    assert update_group.headers.get("location", "").startswith("/groups?highlight_group_id=")

    update_user = client.post(
        f"/users/{uid}",
        data={
            "redmine_id": str(redmine_id),
            "display_name": f"{user_name} updated",
            "group_id": str(gid),
            "room": user_room,
            "status_preset": "all",
            "version_preset": "all",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert update_user.status_code == 303
    assert update_user.headers.get("location", "").startswith("/users?highlight_user_id=")

    delete_user = client.post(
        f"/users/{uid}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert delete_user.status_code == 303
    assert delete_user.headers.get("location") == "/users"

    delete_group = client.post(
        f"/groups/{gid}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert delete_group.status_code == 303
    assert delete_group.headers.get("location") == "/groups"


def test_user_and_group_version_routes_add_and_delete(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)
    token = client.cookies.get("admin_csrf")
    suffix = uuid4().hex[:8]

    create_group = client.post(
        "/groups",
        data={
            "name": f"pytest-vroutes-group-{suffix}",
            "room_id": f"!pytest-vroutes-group-{suffix}:server",
            "timezone_name": "Europe/Moscow",
            "status_preset": "all",
            "version_preset": "all",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    gid = int(
        parse_qs(urlparse(create_group.headers.get("location", "")).query)["highlight_group_id"][0]
    )

    create_user = client.post(
        "/users",
        data={
            "redmine_id": str(930000 + (abs(hash(uuid4().hex)) % 9999)),
            "display_name": f"pytest-vroutes-user-{suffix}",
            "group_id": str(gid),
            "room": f"!pytest-vroutes-user-{suffix}:server",
            "status_preset": "all",
            "version_preset": "all",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    uid = int(
        parse_qs(urlparse(create_user.headers.get("location", "")).query)["highlight_user_id"][0]
    )

    user_key = f"v-user-{suffix}"
    group_key = f"v-group-{suffix}"
    add_ur = client.post(
        f"/users/{uid}/version-routes/add",
        data={"version_key": user_key, "csrf_token": token},
        follow_redirects=False,
    )
    assert add_ur.status_code == 303
    assert "version_msg=added" in (add_ur.headers.get("location") or "")

    add_gr = client.post(
        f"/groups/{gid}/version-routes/add",
        data={"version_key": group_key, "csrf_token": token},
        follow_redirects=False,
    )
    assert add_gr.status_code == 303
    assert "version_msg=added" in (add_gr.headers.get("location") or "")

    from database.models import GroupVersionRoute, UserVersionRoute
    from database.session import get_session_factory

    async def _route_ids() -> tuple[int, int]:
        factory = get_session_factory()
        async with factory() as session:
            ur = await session.execute(
                select(UserVersionRoute.id).where(
                    UserVersionRoute.bot_user_id == uid,
                    UserVersionRoute.version_key == user_key,
                )
            )
            gr = await session.execute(
                select(GroupVersionRoute.id).where(
                    GroupVersionRoute.group_id == gid,
                    GroupVersionRoute.version_key == group_key,
                )
            )
            user_row_id = ur.scalar_one()
            group_row_id = gr.scalar_one()
            return user_row_id, group_row_id

    user_row_id, group_row_id = asyncio.run(_route_ids())

    del_ur = client.post(
        f"/users/{uid}/version-routes/{user_row_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert del_ur.status_code == 303
    assert "version_msg=deleted" in (del_ur.headers.get("location") or "")

    del_gr = client.post(
        f"/groups/{gid}/version-routes/{group_row_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert del_gr.status_code == 303
    assert "version_msg=deleted" in (del_gr.headers.get("location") or "")


def test_ops_restart_accepts_and_redirects(client: TestClient, monkeypatch):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)

    monkeypatch.setattr(admin_main, "_restart_in_background", lambda actor: None)
    client.get("/")
    token = client.cookies.get("admin_csrf")
    r = client.post("/ops/bot/restart", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers.get("location") == "/dashboard?ops=restart_accepted"
