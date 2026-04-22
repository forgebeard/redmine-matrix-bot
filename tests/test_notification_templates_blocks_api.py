"""API code-only редактора шаблонов уведомлений."""

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


def test_removed_block_editor_endpoints_return_404(client: TestClient, _admin_db: None) -> None:
    cases = [
        (
            "POST",
            "/api/bot/notification-templates/compile-blocks",
            {"template_name": "tpl_new_issue", "blocks": []},
        ),
        ("GET", "/api/bot/notification-templates/block-registry", None),
        ("GET", "/api/bot/notification-templates/tpl_new_issue/decompose", None),
        (
            "POST",
            "/api/bot/notification-templates/tpl_new_issue/decompose-body",
            {"body_html": "<p>x</p>"},
        ),
    ]
    for method, url, payload in cases:
        if method == "GET":
            resp = client.get(url)
        else:
            resp = client.post(url, json=payload, headers=_json_headers(client))
        assert resp.status_code == 404, f"{method} {url} should be removed"


def test_preview_code_only_ok(client: TestClient, _admin_db: None) -> None:
    resp = client.post(
        "/api/bot/notification-templates/preview",
        json={"name": "tpl_new_issue", "body_html": "<blockquote>ok {{ issue.id }}</blockquote>"},
        headers=_json_headers(client),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    assert "101" in str(data.get("html") or "")


def test_preview_code_only_error(client: TestClient, _admin_db: None) -> None:
    resp = client.post(
        "/api/bot/notification-templates/preview",
        json={"name": "tpl_new_issue", "body_html": "{{ bad_syntax"},
        headers=_json_headers(client),
    )
    assert resp.status_code == 400
    data = resp.json()
    assert "detail" in data
    assert "Ошибка рендера" in str(data.get("detail", ""))


def test_preview_tpl_test_message_ok(client: TestClient, _admin_db: None) -> None:
    resp = client.post(
        "/api/bot/notification-templates/preview",
        json={
            "name": "tpl_test_message",
            "body_html": "<b>{{ title }}</b><br>{{ message }}<br>{{ scope }}",
            "context": {
                "title": "Тестовое сообщение",
                "message": "Тест из панели",
                "sent_at": "11:22:33",
                "timezone": "Europe/Moscow",
                "scope": "group",
            },
        },
        headers=_json_headers(client),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    assert "Тест из панели" in str(data.get("html") or "")
    assert "group" in str(data.get("html") or "")


def test_default_custom_contract_save_and_reset(client: TestClient, _admin_db: None) -> None:
    token = _csrf(client)
    put_resp = client.put(
        "/api/bot/notification-templates/tpl_new_issue",
        data={"csrf_token": token, "body_html": "<p>custom {{ issue.id }}</p>", "body_plain": ""},
    )
    assert put_resp.status_code == 200

    listing = client.get("/api/bot/notification-templates")
    assert listing.status_code == 200
    row = next(t for t in listing.json().get("templates", []) if t["name"] == "tpl_new_issue")
    assert row["override_html"] is not None
    assert row["default_html"] is not None

    reset_resp = client.post(
        "/api/bot/notification-templates/tpl_new_issue/reset",
        data={"csrf_token": token},
    )
    assert reset_resp.status_code == 200
    listing2 = client.get("/api/bot/notification-templates")
    row2 = next(t for t in listing2.json().get("templates", []) if t["name"] == "tpl_new_issue")
    assert row2["override_html"] is None
    assert row2["default_html"] is not None


def test_notification_templates_list_includes_display_name(
    client: TestClient, _admin_db: None
) -> None:
    resp = client.get("/api/bot/notification-templates")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True
    templates = data.get("templates") or []
    assert len(templates) == len(TEMPLATE_NAMES)
    by_name = {t["name"]: t for t in templates}
    assert "tpl_test_message" in by_name
    assert "tpl_dry_run" not in by_name
    for name in TEMPLATE_NAMES:
        row = by_name[name]
        assert "display_name" in row
        assert row["display_name"] == NOTIFICATION_TEMPLATE_LABELS[name]
