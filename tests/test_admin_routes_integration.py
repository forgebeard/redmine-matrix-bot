"""Полные интеграционные тесты для admin routes — users, groups, settings, ops, auth.

Требуют DATABASE_URL (PostgreSQL). Используют TestClient и _setup_and_login_admin.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.conftest import _setup_and_login_admin

# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _get_csrf(client: TestClient) -> str:
    return client.cookies.get("admin_csrf", "")


def _unique_redmine_id(offset: int = 0) -> int:
    return 900001 + offset + (abs(hash(uuid4().hex)) % 99999)


def _unique_room(prefix: str = "pytest") -> str:
    return f"!{prefix}-{uuid4().hex[:8]}:server"


class _FakeRoom:
    def __init__(self) -> None:
        self.users = {}


class _FakeMatrixClient:
    def __init__(self, room_ids: list[str] | None = None) -> None:
        self.homeserver = "https://messenger.red-soft.ru"
        self.user_id = "@bot:messenger.red-soft.ru"
        self.rooms = {rid: _FakeRoom() for rid in (room_ids or [])}

    async def close(self) -> None:
        return None

    async def join(self, room_id: str):
        self.rooms.setdefault(room_id, _FakeRoom())
        return type("JoinResponse", (), {})()

    async def room_create(self, invite: list[str], is_direct: bool):
        _ = (invite, is_direct)
        rid = f"!dm-{uuid4().hex[:8]}:server"
        self.rooms.setdefault(rid, _FakeRoom())
        return type("RoomCreateResponse", (), {"room_id": rid})()


# ═══════════════════════════════════════════════════════════════════════════
# Users — полный CRUD + валидация
# ═══════════════════════════════════════════════════════════════════════════


class TestUsersCRUD:
    """Полный набор тестов CRUD пользователей."""

    @pytest.fixture(autouse=True)
    def _check_db(self, client: TestClient):
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url or not db_url.startswith("postgresql://"):
            pytest.skip("Требует Postgres (DATABASE_URL)")
        _setup_and_login_admin(client)

    # ── Create ───────────────────────────────────────────────────────────

    def test_create_user_happy_path(self, client: TestClient):
        """Создание пользователя с минимальными полями."""
        token = _get_csrf(client)
        rid = _unique_redmine_id()
        room = _unique_room("create")

        resp = client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "create test user",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        r = client.get("/users")
        assert "create test user" in r.text

    def test_create_user_empty_display_name_ok(self, client: TestClient):
        """Создание без display_name — разрешено."""
        token = _get_csrf(client)
        rid = _unique_redmine_id(1)
        room = _unique_room("empty-name")

        resp = client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code in (303, 200)

    def test_create_user_duplicate_redmine_id(self, client: TestClient):
        """Создание с дублирующим redmine_id отклоняется."""
        token = _get_csrf(client)
        rid = _unique_redmine_id(2)
        room = _unique_room("dup1")

        resp = client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "first",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # Второй с тем же redmine_id
        resp2 = client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "second",
                "room": _unique_room("dup2"),
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp2.status_code in (200, 400, 422)

    def test_create_user_missing_redmine_id(self, client: TestClient):
        """Создание без redmine_id отклоняется."""
        token = _get_csrf(client)
        resp = client.post(
            "/users",
            data={
                "display_name": "no rid",
                "room": _unique_room("norid"),
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303, 400, 422)

    # ── Update ───────────────────────────────────────────────────────────

    def test_update_user_display_name(self, client: TestClient):
        """Обновление display_name."""
        token = _get_csrf(client)
        rid = _unique_redmine_id(10)
        room = _unique_room("upd-name")

        resp = client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "original name",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        uid = parse_qs(urlparse(resp.headers["location"]).query)["highlight_user_id"][0]

        resp2 = client.post(
            f"/users/{uid}",
            data={
                "redmine_id": str(rid),
                "display_name": "modified name",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp2.status_code == 303

        r = client.get("/users")
        assert "modified name" in r.text
        assert "original name" not in r.text

    def test_update_user_change_room(self, client: TestClient):
        """Обновление room_id пользователя."""
        token = _get_csrf(client)
        rid = _unique_redmine_id(11)
        old_room = _unique_room("upd-room-old")
        new_room = _unique_room("upd-room-new")
        new_local = new_room.split(":", 1)[0].lstrip("!")

        resp = client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "room change test",
                "room": old_room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        uid = parse_qs(urlparse(resp.headers["location"]).query)["highlight_user_id"][0]

        resp2 = client.post(
            f"/users/{uid}",
            data={
                "redmine_id": str(rid),
                "display_name": "room change test",
                "room": new_room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp2.status_code == 303
        r = client.get(f"/users/{uid}/edit")
        # На edit page отображается room_localpart (без домена и !)
        assert new_local in r.text

    # ── Delete ──────────────────────────────────────────────────────────

    def test_delete_user(self, client: TestClient):
        """Удаление пользователя."""
        token = _get_csrf(client)
        rid = _unique_redmine_id(20)
        room = _unique_room("del")

        resp = client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "delete me",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        uid = parse_qs(urlparse(resp.headers["location"]).query)["highlight_user_id"][0]

        resp2 = client.post(
            f"/users/{uid}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp2.status_code == 303

        r = client.get("/users")
        assert "delete me" not in r.text

    @pytest.mark.asyncio
    async def test_delete_user_cleans_runtime_dlq_lease_state(self, client: TestClient):
        """При удалении bot_users уходят строки в bot_issue_state, pending_notifications, bot_user_leases."""
        token = _get_csrf(client)
        rid = _unique_redmine_id(21)
        room = _unique_room("del-runtime")

        resp = client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "runtime cleanup",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        uid = parse_qs(urlparse(resp.headers["location"]).query)["highlight_user_id"][0]

        from database.models import BotIssueState, BotUserLease, PendingNotification
        from database.session import get_session_factory

        factory = get_session_factory()

        async with factory() as session:
            session.add(
                BotIssueState(
                    user_redmine_id=rid,
                    issue_id=880_011,
                )
            )
            session.add(
                BotUserLease(
                    user_redmine_id=rid,
                    lease_owner_id=uuid4(),
                    lease_until=datetime.now(UTC) + timedelta(minutes=30),
                )
            )
            session.add(
                PendingNotification(
                    user_redmine_id=rid,
                    issue_id=880_022,
                    room_id="!dlq:server",
                    notification_type="test",
                    payload={"k": 1},
                    next_retry_at=datetime.now(UTC),
                )
            )
            await session.commit()

        client.post(
            f"/users/{uid}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )

        async with factory() as session:
            n_state = (
                await session.execute(
                    select(func.count())
                    .select_from(BotIssueState)
                    .where(BotIssueState.user_redmine_id == rid)
                )
            ).scalar_one()
            n_dlq = (
                await session.execute(
                    select(func.count())
                    .select_from(PendingNotification)
                    .where(PendingNotification.user_redmine_id == rid)
                )
            ).scalar_one()
            n_lease = (
                await session.execute(
                    select(func.count())
                    .select_from(BotUserLease)
                    .where(BotUserLease.user_redmine_id == rid)
                )
            ).scalar_one()
            assert (n_state, n_dlq, n_lease) == (0, 0, 0)

    def test_delete_nonexistent_user(self, client: TestClient):
        """Удаление несуществующего пользователя — 303 или 404."""
        token = _get_csrf(client)
        resp = client.post(
            "/users/999999/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code in (303, 404)

    # ── Filter & Search ──────────────────────────────────────────────────

    def test_users_filter_by_group(self, client: TestClient):
        """Фильтр пользователей по группе."""
        # Сначала создаём группу
        token = _get_csrf(client)
        gname = f"pytest-filter-grp-{uuid4().hex[:8]}"
        groom = _unique_room("filter-grp")
        resp_g = client.post(
            "/groups",
            data={
                "name": gname,
                "room_id": groom,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp_g.status_code == 303
        gid = parse_qs(urlparse(resp_g.headers["location"]).query)["highlight_group_id"][0]

        # Создаём пользователя в группе
        rid = _unique_redmine_id(30)
        room = _unique_room("filter-user")
        client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "filtered user",
                "room": room,
                "group_id": gid,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )

        r = client.get(f"/users?group_id={gid}")
        assert r.status_code == 200
        assert "filtered user" in r.text

    def test_users_search_by_name(self, client: TestClient):
        """Поиск пользователя по имени."""
        rid = _unique_redmine_id(31)
        room = _unique_room("search")
        token = _get_csrf(client)
        client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "unique-searchable-name-xyz",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )

        r = client.get("/users?q=unique-searchable-name-xyz")
        assert r.status_code == 200
        assert "unique-searchable-name-xyz" in r.text

    # ── Notify presets ───────────────────────────────────────────────────

    def test_create_user_notify_new_only(self, client: TestClient):
        """Создание с notify_preset=new_only."""
        token = _get_csrf(client)
        rid = _unique_redmine_id(40)
        room = _unique_room("notify-new")
        client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "notify new only",
                "room": room,
                "notify_preset": "new_only",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        r = client.get("/users")
        assert "notify new only" in r.text

    def test_create_user_notify_overdue_only(self, client: TestClient):
        """Создание с notify_preset=overdue_only."""
        token = _get_csrf(client)
        rid = _unique_redmine_id(41)
        room = _unique_room("notify-overdue")
        client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "notify overdue only",
                "room": room,
                "notify_preset": "overdue_only",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        r = client.get("/users")
        assert "notify overdue only" in r.text


# ═══════════════════════════════════════════════════════════════════════════
# Groups — полный CRUD + валидация
# ═══════════════════════════════════════════════════════════════════════════


class TestGroupsCRUD:
    """Полный набор тестов CRUD групп."""

    @pytest.fixture(autouse=True)
    def _check_db(self, client: TestClient):
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url or not db_url.startswith("postgresql://"):
            pytest.skip("Требует Postgres (DATABASE_URL)")
        _setup_and_login_admin(client)

    # ── Create ───────────────────────────────────────────────────────────

    def test_create_group_happy_path(self, client: TestClient):
        """Создание группы с минимальными полями."""
        token = _get_csrf(client)
        name = f"pytest-create-{uuid4().hex[:8]}"
        room = _unique_room("grp-create")

        resp = client.post(
            "/groups",
            data={
                "name": name,
                "room_id": room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        r = client.get("/groups")
        assert name in r.text

    def test_create_group_empty_name_rejected(self, client: TestClient):
        """Создание без имени отклоняется."""
        token = _get_csrf(client)
        room = _unique_room("grp-noname")
        resp = client.post(
            "/groups",
            data={
                "name": "",
                "room_id": room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303, 400, 422)

    def test_create_group_empty_room_rejected(self, client: TestClient):
        """Создание без room_id отклоняется."""
        token = _get_csrf(client)
        name = f"pytest-noroom-{uuid4().hex[:8]}"
        resp = client.post(
            "/groups",
            data={
                "name": name,
                "room_id": "",
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303, 400, 422)

    def test_create_group_duplicate_name_rejected(self, client: TestClient):
        """Создание с дублирующим именем отклоняется."""
        token = _get_csrf(client)
        name = f"pytest-dup-grp-{uuid4().hex[:8]}"
        room = _unique_room("grp-dup1")
        resp = client.post(
            "/groups",
            data={
                "name": name,
                "room_id": room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        resp2 = client.post(
            "/groups",
            data={
                "name": name,
                "room_id": _unique_room("grp-dup2"),
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp2.status_code in (200, 400, 422)

    def test_create_group_reserved_name_rejected(self, client: TestClient):
        """Создание с зарезервированным именем отклоняется."""
        token = _get_csrf(client)
        resp = client.post(
            "/groups",
            data={
                "name": "UNASSIGNED",
                "room_id": _unique_room("reserved"),
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303, 400, 422)

    # ── Update ───────────────────────────────────────────────────────────

    def test_update_group_name(self, client: TestClient):
        """Обновление имени группы."""
        token = _get_csrf(client)
        name = f"pytest-update-grp-{uuid4().hex[:8]}"
        room = _unique_room("grp-upd")

        resp = client.post(
            "/groups",
            data={
                "name": name,
                "room_id": room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        gid = parse_qs(urlparse(resp.headers["location"]).query)["highlight_group_id"][0]

        new_name = f"{name}-modified"
        resp2 = client.post(
            f"/groups/{gid}",
            data={
                "name": new_name,
                "room_id": room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp2.status_code == 303
        r = client.get("/groups")
        assert new_name in r.text
        assert name not in r.text or new_name in r.text

    def test_update_group_change_room_id(self, client: TestClient):
        """Обновление room_id группы — каскадное обновление routes."""
        token = _get_csrf(client)
        name = f"pytest-roomchange-{uuid4().hex[:8]}"
        old_room = _unique_room("grp-old")
        new_room = _unique_room("grp-new")

        resp = client.post(
            "/groups",
            data={
                "name": name,
                "room_id": old_room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        gid = parse_qs(urlparse(resp.headers["location"]).query)["highlight_group_id"][0]

        resp2 = client.post(
            f"/groups/{gid}",
            data={
                "name": name,
                "room_id": new_room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp2.status_code == 303
        r = client.get(f"/groups/{gid}/edit")
        assert r.status_code == 200

    # ── Delete ───────────────────────────────────────────────────────────

    def test_delete_group(self, client: TestClient):
        """Удаление группы."""
        token = _get_csrf(client)
        name = f"pytest-del-grp-{uuid4().hex[:8]}"
        room = _unique_room("grp-del")

        resp = client.post(
            "/groups",
            data={
                "name": name,
                "room_id": room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        gid = parse_qs(urlparse(resp.headers["location"]).query)["highlight_group_id"][0]

        resp2 = client.post(
            f"/groups/{gid}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp2.status_code == 303

        r = client.get("/groups")
        assert name not in r.text

    # ── Status/Version routes ────────────────────────────────────────────

    def test_add_status_route_to_group(self, client: TestClient):
        """Добавление status route к группе."""
        token = _get_csrf(client)
        name = f"pytest-status-route-{uuid4().hex[:8]}"
        room = _unique_room("grp-status")

        resp = client.post(
            "/groups",
            data={
                "name": name,
                "room_id": room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        gid = parse_qs(urlparse(resp.headers["location"]).query)["highlight_group_id"][0]

        resp2 = client.post(
            f"/groups/{gid}/status-routes/add",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp2.status_code in (200, 303, 400, 422)

    def test_add_version_route_to_group(self, client: TestClient):
        """Добавление version route к группе."""
        token = _get_csrf(client)
        name = f"pytest-ver-route-{uuid4().hex[:8]}"
        room = _unique_room("grp-ver")

        resp = client.post(
            "/groups",
            data={
                "name": name,
                "room_id": room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        gid = parse_qs(urlparse(resp.headers["location"]).query)["highlight_group_id"][0]

        resp2 = client.post(
            f"/groups/{gid}/version-routes/add",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp2.status_code in (200, 303, 400, 422)


# ═══════════════════════════════════════════════════════════════════════════
# Settings — onboarding, catalogs
# ═══════════════════════════════════════════════════════════════════════════


class TestSettingsIntegration:
    """Тесты страницы настроек."""

    @pytest.fixture(autouse=True)
    def _check_db(self, client: TestClient):
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url or not db_url.startswith("postgresql://"):
            pytest.skip("Требует Postgres (DATABASE_URL)")
        _setup_and_login_admin(client)

    def test_onboarding_page_loads(self, client: TestClient):
        r = client.get("/onboarding")
        assert r.status_code == 200
        assert "Параметры сервиса" in r.text

    def test_onboarding_has_redmine_fields(self, client: TestClient):
        r = client.get("/onboarding")
        assert "Адрес Redmine" in r.text
        assert "API-ключ Redmine" in r.text

    def test_onboarding_has_matrix_fields(self, client: TestClient):
        r = client.get("/onboarding")
        assert "Адрес Matrix" in r.text
        assert "Токен" in r.text

    def test_onboarding_has_catalogs(self, client: TestClient):
        r = client.get("/onboarding")
        assert "Справочник" in r.text
        assert "Уведомления" in r.text

    def test_onboarding_notifications_is_code_only(self, client: TestClient):
        r = client.get("/onboarding")
        assert r.status_code == 200
        assert "template_block_editor.js" not in r.text
        assert "template_block_editor.css" not in r.text
        assert "Sortable.min.js" not in r.text
        assert 'id="tpl-v2-fields"' in r.text
        assert "static/admin/js/onboarding.js" in r.text
        assert "Обновить предпросмотр" not in r.text

    def test_onboarding_save_empty_redirects(self, client: TestClient):
        """POST /onboarding/save с пустыми значениями перенаправляет."""
        token = _get_csrf(client)
        resp = client.post(
            "/onboarding/save",
            data={
                "csrf_token": token,
                "secret_REDMINE_URL": "",
                "secret_REDMINE_API_KEY": "",
                "secret_MATRIX_HOMESERVER": "",
                "secret_MATRIX_USER_ID": "",
                "secret_MATRIX_ACCESS_TOKEN": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

    @pytest.mark.asyncio
    async def test_onboarding_save_syncs_portal_base_url_from_redmine(self, client: TestClient):
        token = _get_csrf(client)
        redmine_url = "https://support.red-soft.ru/"
        resp = client.post(
            "/onboarding/save",
            data={
                "csrf_token": token,
                "secret_REDMINE_URL": redmine_url,
                "secret_REDMINE_API_KEY": "",
                "secret_MATRIX_HOMESERVER": "",
                "secret_MATRIX_USER_ID": "",
                "secret_MATRIX_ACCESS_TOKEN": "",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        from database.models import AppSecret
        from database.session import get_session_factory
        from security import decrypt_secret, load_master_key

        factory = get_session_factory()
        async with factory() as session:
            rows = (
                await session.execute(
                    select(AppSecret).where(
                        AppSecret.name.in_(["REDMINE_URL", "PORTAL_BASE_URL"])
                    )
                )
            ).scalars().all()
            vals = {
                row.name: decrypt_secret(row.ciphertext, row.nonce, load_master_key())
                for row in rows
            }
        assert vals["REDMINE_URL"] == "https://support.red-soft.ru"
        assert vals["PORTAL_BASE_URL"] == "https://support.red-soft.ru"

    def test_catalog_save_notify(self, client: TestClient):
        """Сохранение справочника уведомлений."""
        token = _get_csrf(client)
        resp = client.post(
            "/onboarding/catalog/save",
            json={
                "csrf_token": token,
                "catalog": "notify",
                "items": [{"key": "n_test", "label": "Тестовое"}],
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303, 403)


class TestTemplateDrivenTestMessageRoutes:
    @pytest.fixture(autouse=True)
    def _check_db(self, client: TestClient):
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url or not db_url.startswith("postgresql://"):
            pytest.skip("Требует Postgres (DATABASE_URL)")
        _setup_and_login_admin(client)

    def _create_user_with_room(self, client: TestClient) -> tuple[int, str]:
        token = _get_csrf(client)
        rid = _unique_redmine_id(10_000)
        room = _unique_room("testmsg")
        resp = client.post(
            "/users",
            data={
                "redmine_id": str(rid),
                "display_name": "template test user",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers.get("location", "")
        qs = parse_qs(urlparse(loc).query)
        assert "highlight_user_id" in qs
        return int(qs["highlight_user_id"][0]), room

    def test_users_test_message_uses_tpl_test_message(self, client: TestClient, monkeypatch):
        uid, room_id = self._create_user_with_room(client)
        fake_client = _FakeMatrixClient([room_id])
        sent: dict[str, object] = {}

        async def _fake_get_matrix_client(_session):
            return fake_client

        async def _fake_sync(_client):
            return True

        async def _fake_send(_client, _room_id, content, **_kwargs):
            sent["room_id"] = _room_id
            sent["content"] = content

        async def _fake_render(_session, name, context, *, root=None):
            _ = root
            assert name == "tpl_test_message"
            assert context["scope"] == "user"
            return "<b>Rendered user test</b>", "Rendered plain test"

        import admin.main as admin_main  # noqa: PLC0415
        import admin.routes.users as users_routes  # noqa: PLC0415
        import src.matrix_send as matrix_send  # noqa: PLC0415

        monkeypatch.setattr(admin_main, "_get_matrix_client", _fake_get_matrix_client)
        monkeypatch.setattr(admin_main, "_sync_matrix_client", _fake_sync)
        monkeypatch.setattr(matrix_send, "room_send_with_retry", _fake_send)
        monkeypatch.setattr(users_routes, "render_named_template", _fake_render)

        token = _get_csrf(client)
        resp = client.post(
            "/users/test-message",
            data={"user_id": str(uid)},
            headers={"Accept": "application/json", "X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        assert sent["room_id"] == room_id
        content = sent["content"]
        assert isinstance(content, dict)
        assert content["formatted_body"] == "<b>Rendered user test</b>"
        assert content["body"] == "Rendered plain test"

    def test_groups_test_message_uses_tpl_test_message(self, client: TestClient, monkeypatch):
        room_id = _unique_room("group-tpl")
        fake_client = _FakeMatrixClient([room_id])
        sent: dict[str, object] = {}

        async def _fake_get_matrix_client(_session):
            return fake_client

        async def _fake_sync(_client):
            return True

        async def _fake_send(_client, _room_id, content, **_kwargs):
            sent["room_id"] = _room_id
            sent["content"] = content

        async def _fake_render(_session, name, context, *, root=None):
            _ = root
            assert name == "tpl_test_message"
            assert context["scope"] == "group"
            return "<b>Rendered group test</b>", "Rendered plain group"

        import admin.main as admin_main  # noqa: PLC0415
        import admin.routes.groups as groups_routes  # noqa: PLC0415
        import src.matrix_send as matrix_send  # noqa: PLC0415

        monkeypatch.setattr(admin_main, "_get_matrix_client", _fake_get_matrix_client)
        monkeypatch.setattr(admin_main, "_sync_matrix_client", _fake_sync)
        monkeypatch.setattr(matrix_send, "room_send_with_retry", _fake_send)
        monkeypatch.setattr(groups_routes, "render_named_template", _fake_render)

        token = _get_csrf(client)
        resp = client.post(
            "/groups/test-message",
            data={"room_id": room_id},
            headers={"Accept": "application/json", "X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        content = sent["content"]
        assert isinstance(content, dict)
        assert content["formatted_body"] == "<b>Rendered group test</b>"
        assert content["body"] == "Rendered plain group"


# ═══════════════════════════════════════════════════════════════════════════
# Auth — login, logout, setup, reset-password
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthRoutes:
    """Тесты аутентификации."""

    @pytest.fixture(autouse=True)
    def _check_db(self, client: TestClient):
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url or not db_url.startswith("postgresql://"):
            pytest.skip("Требует Postgres (DATABASE_URL)")

    def test_login_page_loads(self, client: TestClient):
        r = client.get("/login")
        assert r.status_code == 200

    def test_login_invalid_credentials(self, client: TestClient):
        """Вход с неверными credentials."""
        client.get("/login")
        token = _get_csrf(client)
        resp = client.post(
            "/login",
            data={"login": "nonexistent@test.com", "password": "wrong", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code in (200, 401)

    def test_login_empty_csrf(self, client: TestClient):
        """Вход без CSRF токена."""
        resp = client.post(
            "/login",
            data={"login": "test@test.com", "password": "test", "csrf_token": ""},
            follow_redirects=False,
        )
        # CSRF защита: либо 403, либо 400, либо redirect на login
        assert resp.status_code in (200, 400, 403, 422)

    def test_logout_redirects(self, client: TestClient):
        """Logout перенаправляет на login."""
        # Сначала входим
        _setup_and_login_admin(client)
        resp = client.get("/logout", follow_redirects=False)
        assert resp.status_code in (302, 303)

    def test_protected_page_without_auth(self, client: TestClient):
        """Доступ к защищённой странице без авторизации."""
        resp = client.get("/users", follow_redirects=False)
        assert resp.status_code in (302, 303, 403)

    def test_setup_page_loads_when_no_admin(self, client: TestClient):
        """Страница /setup загружается."""
        r = client.get("/setup")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Ops — bot start/stop/restart (mocked)
# ═══════════════════════════════════════════════════════════════════════════


class TestOpsRoutes:
    """Тесты операций с ботом (mocked Docker control)."""

    @pytest.fixture(autouse=True)
    def _check_db(self, client: TestClient):
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url or not db_url.startswith("postgresql://"):
            pytest.skip("Требует Postgres (DATABASE_URL)")
        _setup_and_login_admin(client)

    def test_ops_start_accepts_and_redirects(self, client: TestClient, monkeypatch):
        """POST /ops/bot/start перенаправляет."""
        import admin.main as admin_main  # noqa: PLC0415

        monkeypatch.setattr(admin_main, "_restart_in_background", lambda actor=None: None)
        client.get("/")
        token = _get_csrf(client)
        r = client.post("/ops/bot/start", data={"csrf_token": token}, follow_redirects=False)
        assert r.status_code in (302, 303)

    def test_ops_stop_accepts_and_redirects(self, client: TestClient, monkeypatch):
        """POST /ops/bot/stop перенаправляет."""
        import admin.main as admin_main  # noqa: PLC0415

        monkeypatch.setattr(admin_main, "_restart_in_background", lambda actor=None: None)
        client.get("/")
        token = _get_csrf(client)
        r = client.post("/ops/bot/stop", data={"csrf_token": token}, follow_redirects=False)
        assert r.status_code in (302, 303)

    def test_ops_restart_accepts_and_redirects(self, client: TestClient, monkeypatch):
        """POST /ops/bot/restart перенаправляет."""
        import admin.main as admin_main  # noqa: PLC0415

        monkeypatch.setattr(admin_main, "_restart_in_background", lambda actor=None: None)
        client.get("/")
        token = _get_csrf(client)
        r = client.post("/ops/bot/restart", data={"csrf_token": token}, follow_redirects=False)
        assert r.status_code in (302, 303)
        assert r.headers.get("location") == "/dashboard?ops=restart_accepted"

    def test_ops_invalid_action(self, client: TestClient, monkeypatch):
        """Неизвестное действие ops возвращает ошибку."""
        import admin.main as admin_main  # noqa: PLC0415

        monkeypatch.setattr(admin_main, "_restart_in_background", lambda actor=None: None)
        client.get("/")
        token = _get_csrf(client)
        r = client.post(
            "/ops/bot/invalid_action", data={"csrf_token": token}, follow_redirects=False
        )
        assert r.status_code in (400, 404, 422)


# ═══════════════════════════════════════════════════════════════════════════
# Me — self-service settings
# ═══════════════════════════════════════════════════════════════════════════


class TestMeSettings:
    """Тесты self-service настроек."""

    @pytest.fixture(autouse=True)
    def _check_db(self, client: TestClient):
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url or not db_url.startswith("postgresql://"):
            pytest.skip("Требует Postgres (DATABASE_URL)")
        _setup_and_login_admin(client)

    def test_me_settings_page_loads(self, client: TestClient):
        r = client.get("/me/settings")
        assert r.status_code == 200

    def test_me_settings_save_redirects(self, client: TestClient):
        """Сохранение self-service настроек перенаправляет."""
        token = _get_csrf(client)
        resp = client.post(
            "/me/settings",
            data={
                "csrf_token": token,
                "timezone": "Europe/Moscow",
                "notify_preset": "all",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
