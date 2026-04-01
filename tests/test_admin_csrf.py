"""
Проверки double-submit CSRF для админки: unit (_verify_csrf) + POST /login при наличии Postgres.
"""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

os.environ.setdefault("APP_MASTER_KEY", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("SMTP_MOCK", "1")

import admin.constants as admin_constants  # noqa: E402
import admin.csrf as admin_csrf  # noqa: E402
import admin_main  # noqa: E402


def _request_with_cookies(
    cookies: dict[str, str],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    hdrs: list[tuple[bytes, bytes]] = []
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        hdrs.append((b"cookie", cookie_str.encode("utf-8")))
    if extra_headers:
        hdrs.extend(extra_headers)
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.0", "version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "root_path": "",
        "scheme": "http",
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "headers": hdrs,
    }
    return Request(scope)


def test_verify_csrf_accepts_matching_form_token() -> None:
    tok = "csrf_form_ok_1"
    req = _request_with_cookies({admin_constants.CSRF_COOKIE_NAME: tok})
    admin_csrf.verify_csrf(req, tok)


def test_verify_csrf_accepts_matching_htmx_header() -> None:
    tok = "csrf_htmx_header_ok"
    req = _request_with_cookies(
        {admin_constants.CSRF_COOKIE_NAME: tok},
        extra_headers=[(b"x-csrf-token", tok.encode("utf-8"))],
    )
    admin_csrf.verify_csrf(req, "")


def test_verify_csrf_rejects_without_cookie() -> None:
    req = _request_with_cookies({})
    with pytest.raises(HTTPException) as exc_info:
        admin_csrf.verify_csrf(req, "any-token")
    assert exc_info.value.status_code == 400
    assert "CSRF" in (exc_info.value.detail or "")


def test_verify_csrf_rejects_mismatched_token() -> None:
    tok = "cookie_value"
    req = _request_with_cookies({admin_constants.CSRF_COOKIE_NAME: tok})
    with pytest.raises(HTTPException) as exc_info:
        admin_csrf.verify_csrf(req, "other_value")
    assert exc_info.value.status_code == 400


@pytest.fixture
def client() -> TestClient:
    return TestClient(admin_main.app)


def test_login_post_rejects_wrong_csrf_with_db(client: TestClient) -> None:
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    client.get("/login")
    r = client.post(
        "/login",
        data={
            "login": "nobody",
            "password": "DoesNotMatter123",
            "csrf_token": "definitely_wrong",
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert "detail" in body
    assert "CSRF" in (body.get("detail") or "")


def test_login_post_with_valid_csrf_proceeds_to_auth_not_csrf_error(client: TestClient) -> None:
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url or not db_url.startswith("postgresql://"):
        pytest.skip("Тест требует Postgres (DATABASE_URL)")
    page = client.get("/login")
    token = page.cookies.get(admin_constants.CSRF_COOKIE_NAME)
    assert token
    r = client.post(
        "/login",
        data={
            "login": "unknown_user",
            "password": "StrongPassword123",
            "csrf_token": token,
        },
    )
    assert r.status_code == 401
    assert "CSRF" not in (r.text or "")
