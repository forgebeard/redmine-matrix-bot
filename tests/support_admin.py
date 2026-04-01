"""Вспомогательные функции для тестов админки (логин / первичная смена учётки)."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from admin.constants import MUST_CHANGE_CREDENTIALS_PATH


def _csrf_from_form(html: str) -> str | None:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'value="([^"]+)"\s+name="csrf_token"', html)
    return m.group(1) if m else None


def ensure_admin_logged_in(
    client: TestClient,
    final_login: str = "test_admin",
    final_password: str = "StrongPassword123",
) -> None:
    """
    Входит в панель: при необходимости admin/admin → форма смены учётки → final_login/final_password.
    Если учётка уже сменена, выполняет вход по final_login.
    """
    lp = client.get("/login")
    assert lp.status_code == 200
    tok = lp.cookies.get("admin_csrf")
    assert tok, "ожидается CSRF cookie на /login"

    r = client.post(
        "/login",
        data={"login": "admin", "password": "admin", "csrf_token": tok},
        follow_redirects=False,
    )
    loc = r.headers.get("location", "")

    if r.status_code in (302, 303) and MUST_CHANGE_CREDENTIALS_PATH in loc:
        pg = client.get(MUST_CHANGE_CREDENTIALS_PATH)
        assert pg.status_code == 200, pg.text[:500]
        form_csrf = _csrf_from_form(pg.text) or client.cookies.get("admin_csrf")
        assert form_csrf
        r2 = client.post(
            MUST_CHANGE_CREDENTIALS_PATH,
            data={
                "login": final_login,
                "password": final_password,
                "password_confirm": final_password,
                "csrf_token": form_csrf,
            },
            follow_redirects=True,
        )
        assert r2.status_code == 200, r2.text[:500]
        return

    if r.status_code == 401:
        lp2 = client.get("/login")
        tok2 = lp2.cookies.get("admin_csrf")
        assert tok2
        r3 = client.post(
            "/login",
            data={
                "login": final_login,
                "password": final_password,
                "csrf_token": tok2,
            },
            follow_redirects=True,
        )
        assert r3.status_code == 200
        return

    if r.status_code in (302, 303):
        # Сессия есть, обязательная смена не требуется — открываем целевой URL редиректа
        path = loc
        for prefix in ("http://testserver", "https://testserver"):
            if path.startswith(prefix):
                path = path[len(prefix) :] or "/"
                break
        client.get(path if path.startswith("/") else f"/{path}")
        return

    raise AssertionError(f"Неожиданный ответ логина: {r.status_code} {loc!r}")
