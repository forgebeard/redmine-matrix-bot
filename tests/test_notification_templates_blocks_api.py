"""API блокового редактора шаблонов уведомлений."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from database.notification_template_repo import NOTIFICATION_TEMPLATE_LABELS, TEMPLATE_NAMES
from tests.conftest import _setup_and_login_admin


def _csrf(client: TestClient) -> str:
    return client.cookies.get("admin_csrf", "")


def _json_headers(client: TestClient) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRF-Token": _csrf(client),
    }


@pytest.fixture
def _admin_db(client: TestClient):
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Требует Postgres (DATABASE_URL)")
    _setup_and_login_admin(client)


def test_compile_blocks_success(client: TestClient, _admin_db: None) -> None:
    resp = client.post(
        "/api/bot/notification-templates/compile-blocks",
        json={
            "template_name": "tpl_new_issue",
            "blocks": [
                {
                    "block_id": "new_issue_header",
                    "enabled": True,
                    "order": 0,
                    "settings": {"emoji": "🆕"},
                },
                {"block_id": "issue_subject", "enabled": True, "order": 1, "settings": {}},
                {"block_id": "new_issue_status", "enabled": True, "order": 2, "settings": {}},
            ],
        },
        headers=_json_headers(client),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    assert "jinja" in data
    assert data.get("html_preview")


def test_compile_blocks_unknown_block_400(client: TestClient, _admin_db: None) -> None:
    resp = client.post(
        "/api/bot/notification-templates/compile-blocks",
        json={
            "template_name": "tpl_new_issue",
            "blocks": [
                {"block_id": "evil", "enabled": True, "order": 0, "settings": {}},
            ],
        },
        headers=_json_headers(client),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body.get("ok") is False
    assert "Unknown" in (body.get("error") or "")


def test_compile_blocks_bad_template_400(client: TestClient, _admin_db: None) -> None:
    resp = client.post(
        "/api/bot/notification-templates/compile-blocks",
        json={"template_name": "tpl_not_a_real_template", "blocks": []},
        headers=_json_headers(client),
    )
    assert resp.status_code == 400


def test_compile_blocks_csrf_400(client: TestClient, _admin_db: None) -> None:
    resp = client.post(
        "/api/bot/notification-templates/compile-blocks",
        json={"template_name": "tpl_new_issue", "blocks": []},
        headers={"Content-Type": "application/json", "Accept": "application/json", "X-CSRF-Token": "bad"},
    )
    assert resp.status_code == 400


def test_compile_blocks_guest_rejected() -> None:
    """Без входа в админку — отказ (CSRF и/или роль). Новый клиент без cookies."""
    import admin.main as admin_main  # noqa: PLC0415

    with TestClient(admin_main.app) as c:
        resp = c.post(
            "/api/bot/notification-templates/compile-blocks",
            json={"template_name": "tpl_new_issue", "blocks": []},
            headers={"Content-Type": "application/json", "X-CSRF-Token": "x"},
            follow_redirects=False,
        )
    assert resp.status_code in (302, 303, 400, 403)


def test_decompose_get(client: TestClient, _admin_db: None) -> None:
    resp = client.get("/api/bot/notification-templates/tpl_new_issue/decompose")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    assert data.get("blocks") is not None
    assert data.get("is_custom_jinja") is False
    assert len(data.get("default_blocks") or []) >= 1


def test_decompose_digest_ok(client: TestClient, _admin_db: None) -> None:
    resp = client.get("/api/bot/notification-templates/tpl_digest/decompose")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    assert data.get("is_custom_jinja") is False
    assert data.get("blocks")


def test_decompose_body_roundtrip(client: TestClient, _admin_db: None) -> None:
    cr = client.post(
        "/api/bot/notification-templates/compile-blocks",
        json={
            "template_name": "tpl_task_change",
            "blocks": [
                {
                    "block_id": "task_change_header",
                    "enabled": True,
                    "order": 0,
                    "settings": {"emoji": "📝", "title": "Изменение"},
                },
                {"block_id": "issue_subject", "enabled": True, "order": 1, "settings": {}},
                {"block_id": "task_change_event", "enabled": True, "order": 2, "settings": {}},
            ],
        },
        headers=_json_headers(client),
    )
    assert cr.status_code == 200
    jinja = cr.json()["jinja"]
    resp = client.post(
        "/api/bot/notification-templates/tpl_task_change/decompose-body",
        json={"body_html": jinja},
        headers=_json_headers(client),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("blocks") is not None
    assert data.get("is_custom_jinja") is False


def test_decompose_body_custom(client: TestClient, _admin_db: None) -> None:
    resp = client.post(
        "/api/bot/notification-templates/tpl_new_issue/decompose-body",
        json={"body_html": "<div>{{ totally_custom }}</div>"},
        headers=_json_headers(client),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("blocks") is None
    assert data.get("is_custom_jinja") is True


def test_block_registry(client: TestClient, _admin_db: None) -> None:
    resp = client.get("/api/bot/notification-templates/block-registry")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    blocks = data.get("blocks") or []
    assert isinstance(blocks, list)
    assert len(blocks) > 0
    assert "settings_schema" in blocks[0]


def test_notification_templates_list_includes_display_name(client: TestClient, _admin_db: None) -> None:
    resp = client.get("/api/bot/notification-templates")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    templates = data.get("templates") or []
    assert len(templates) == len(TEMPLATE_NAMES)
    by_name = {t["name"]: t for t in templates}
    for name in TEMPLATE_NAMES:
        row = by_name[name]
        assert "display_name" in row
        assert row["display_name"] == NOTIFICATION_TEMPLATE_LABELS[name]
