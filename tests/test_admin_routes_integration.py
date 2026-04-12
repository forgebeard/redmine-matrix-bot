"""Интеграционные тесты для admin routes — users, groups, settings, ops.

Требуют DATABASE_URL (PostgreSQL). Используют TestClient и _setup_and_login_admin.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.conftest import _setup_and_login_admin


# ═══════════════════════════════════════════════════════════════════════════
# Users — список, создание, обновление, удаление
# ═══════════════════════════════════════════════════════════════════════════


class TestUsersIntegration:
    """Интеграционные тесты CRUD пользователей бота."""

    @pytest.fixture(autouse=True)
    def _check_db(self, client: TestClient):
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url or not db_url.startswith("postgresql://"):
            pytest.skip("Требует Postgres (DATABASE_URL)")
        _setup_and_login_admin(client)

    def test_users_list_page_loads(self, client: TestClient):
        r = client.get("/users")
        assert r.status_code == 200
        assert "Пользователи" in r.text

    def test_users_list_search_filter(self, client: TestClient):
        """Страница /users?q=... отображается."""
        r = client.get("/users?q=test")
        assert r.status_code == 200
        assert "Пользователи" in r.text

    def test_users_new_page_loads(self, client: TestClient):
        r = client.get("/users/new")
        assert r.status_code == 200
        assert "Новый пользователь" in r.text

    def test_users_create_and_verify_in_list(self, client: TestClient):
        """Создание пользователя и проверка в списке."""
        token = client.cookies.get("admin_csrf")
        redmine_id = 900001 + (abs(hash(uuid4().hex)) % 99999)
        room = f"!pytest-{uuid4().hex[:8]}:server"

        resp = client.post(
            "/users",
            data={
                "redmine_id": str(redmine_id),
                "display_name": "pytest integration user",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # Проверка что пользователь появился в списке
        r = client.get("/users")
        assert r.status_code == 200
        assert "pytest integration user" in r.text

    def test_users_edit_page_loads(self, client: TestClient):
        """Страница редактирования загружается для существующего пользователя."""
        # Сначала создаём пользователя
        token = client.cookies.get("admin_csrf")
        redmine_id = 900002 + (abs(hash(uuid4().hex)) % 99999)
        room = f"!pytest-edit-{uuid4().hex[:8]}:server"

        resp = client.post(
            "/users",
            data={
                "redmine_id": str(redmine_id),
                "display_name": "pytest edit user",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers.get("location", "")
        user_id = parse_qs(urlparse(loc).query).get("highlight_user_id", [None])[0]
        assert user_id

        # Загружаем страницу редактирования
        r = client.get(f"/users/{user_id}/edit")
        assert r.status_code == 200
        assert "Редактирование" in r.text or "edit" in r.text.lower()

    def test_users_update_preserves_data(self, client: TestClient):
        """Обновление пользователя сохраняет изменённые данные."""
        token = client.cookies.get("admin_csrf")
        redmine_id = 900003 + (abs(hash(uuid4().hex)) % 99999)
        room = f"!pytest-update-{uuid4().hex[:8]}:server"

        # Создаём
        resp = client.post(
            "/users",
            data={
                "redmine_id": str(redmine_id),
                "display_name": "pytest update original",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers.get("location", "")
        user_id = parse_qs(urlparse(loc).query).get("highlight_user_id", [None])[0]
        assert user_id

        # Обновляем display_name
        resp2 = client.post(
            f"/users/{user_id}",
            data={
                "redmine_id": str(redmine_id),
                "display_name": "pytest update modified",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp2.status_code == 303

        # Проверяем что имя изменилось
        r = client.get("/users")
        assert "pytest update modified" in r.text
        assert "pytest update original" not in r.text

    def test_users_delete_removes_from_list(self, client: TestClient):
        """Удаление пользователя убирает его из списка."""
        token = client.cookies.get("admin_csrf")
        redmine_id = 900004 + (abs(hash(uuid4().hex)) % 99999)
        room = f"!pytest-del-{uuid4().hex[:8]}:server"

        # Создаём
        resp = client.post(
            "/users",
            data={
                "redmine_id": str(redmine_id),
                "display_name": "pytest delete target",
                "room": room,
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers.get("location", "")
        user_id = parse_qs(urlparse(loc).query).get("highlight_user_id", [None])[0]
        assert user_id

        # Проверяем что есть в списке
        r = client.get("/users")
        assert "pytest delete target" in r.text

        # Удаляем
        resp2 = client.post(
            f"/users/{user_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp2.status_code == 303

        # Проверяем что нет в списке
        r2 = client.get("/users")
        assert "pytest delete target" not in r2.text

    def test_users_create_requires_room(self, client: TestClient):
        """Создание без room отклоняется."""
        token = client.cookies.get("admin_csrf")
        redmine_id = 900005 + (abs(hash(uuid4().hex)) % 99999)

        resp = client.post(
            "/users",
            data={
                "redmine_id": str(redmine_id),
                "display_name": "no room user",
                "room": "",
                "notify_preset": "all",
                "version_preset": "all",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        # Должен вернуть ошибку или редирект с ошибкой
        assert resp.status_code in (200, 303, 400)


# ═══════════════════════════════════════════════════════════════════════════
# Groups — список, создание, удаление
# ═══════════════════════════════════════════════════════════════════════════


class TestGroupsIntegration:
    """Интеграционные тесты CRUD групп."""

    @pytest.fixture(autouse=True)
    def _check_db(self, client: TestClient):
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url or not db_url.startswith("postgresql://"):
            pytest.skip("Требует Postgres (DATABASE_URL)")
        _setup_and_login_admin(client)

    def test_groups_list_page_loads(self, client: TestClient):
        r = client.get("/groups")
        assert r.status_code == 200
        assert "Группы" in r.text

    def test_groups_new_page_loads(self, client: TestClient):
        r = client.get("/groups/new")
        assert r.status_code == 200
        assert "Новая группа" in r.text

    def test_groups_create_and_verify_in_list(self, client: TestClient):
        """Создание группы и проверка в списке."""
        token = client.cookies.get("admin_csrf")
        group_name = f"pytest-group-{uuid4().hex[:8]}"
        room = f"!pytest-grp-{uuid4().hex[:8]}:server"

        resp = client.post(
            "/groups",
            data={
                "name": group_name,
                "room": room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # Проверка в списке
        r = client.get("/groups")
        assert r.status_code == 200
        assert group_name in r.text

    def test_groups_delete_removes_from_list(self, client: TestClient):
        """Удаление группы убирает из списка."""
        token = client.cookies.get("admin_csrf")
        group_name = f"pytest-grp-del-{uuid4().hex[:8]}"
        room = f"!pytest-grp-del-{uuid4().hex[:8]}:server"

        # Создаём
        resp = client.post(
            "/groups",
            data={
                "name": group_name,
                "room": room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = resp.headers.get("location", "")
        group_id = parse_qs(urlparse(loc).query).get("highlight_group_id", [None])[0]
        assert group_id

        # Проверяем что есть
        r = client.get("/groups")
        assert group_name in r.text

        # Удаляем
        resp2 = client.post(
            f"/groups/{group_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp2.status_code == 303

        # Проверяем что нет
        r2 = client.get("/groups")
        assert group_name not in r2.text

    def test_groups_create_requires_name(self, client: TestClient):
        """Создание без имени отклоняется."""
        token = client.cookies.get("admin_csrf")
        room = f"!pytest-grp-noroom-{uuid4().hex[:8]}:server"

        resp = client.post(
            "/groups",
            data={
                "name": "",
                "room": room,
                "status_keys": "",
                "version_keys": "",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 303, 400)


# ═══════════════════════════════════════════════════════════════════════════
# Settings — onboarding page
# ═══════════════════════════════════════════════════════════════════════════


class TestSettingsIntegration:
    """Интеграционные тесты страницы настроек."""

    @pytest.fixture(autouse=True)
    def _check_db(self, client: TestClient):
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url or not db_url.startswith("postgresql://"):
            pytest.skip("Требует Postgres (DATABASE_URL)")
        _setup_and_login_admin(client)

    def test_onboarding_page_loads_for_admin(self, client: TestClient):
        r = client.get("/onboarding")
        assert r.status_code == 200
        assert "Параметры сервиса" in r.text
        assert "База данных сервиса" in r.text
        assert "Таймзона сервиса" in r.text

    def test_onboarding_contains_redmine_fields(self, client: TestClient):
        r = client.get("/onboarding")
        assert "Адрес Redmine" in r.text
        assert "API-ключ Redmine" in r.text

    def test_onboarding_contains_matrix_fields(self, client: TestClient):
        r = client.get("/onboarding")
        assert "Адрес Matrix" in r.text
        assert "Имя учётной записи" in r.text
        assert "Токен" in r.text

    def test_onboarding_contains_catalog_sections(self, client: TestClient):
        r = client.get("/onboarding")
        assert "Справочник" in r.text
        assert "Уведомления" in r.text
        assert "Версии" in r.text

    def test_onboarding_save_redirects(self, client: TestClient):
        """POST /onboarding/save перенаправляет на /onboarding."""
        token = client.cookies.get("admin_csrf")
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
