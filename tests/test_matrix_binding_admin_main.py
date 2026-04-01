import os
import re

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APP_MASTER_KEY", "0123456789abcdef0123456789abcdef")
os.environ.pop("ADMIN_LOGINS", None)

import admin_main


def _db_ready() -> bool:
    db_url = os.getenv("DATABASE_URL", "")
    return bool(db_url) and db_url.startswith("postgresql://")


@pytest.fixture
def client():
    return TestClient(admin_main.app)


def test_matrix_bind_redirects_to_login_without_auth(client: TestClient):
    r = client.get("/matrix/bind", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    loc = r.headers.get("location", "")
    assert loc.endswith("/login") or loc.endswith("/setup"), loc


@pytest.mark.skipif(not _db_ready(), reason="DB auth требует DATABASE_URL (postgresql://...)")
def test_matrix_bind_flow_dev_echo_updates_bot_user_room(client: TestClient, monkeypatch):
    # test_bot.py задаёт MATRIX_* через setdefault — иначе /matrix/bind/start шлёт код в
    # «тестовый» homeserver и nio зависает на таймаутах на весь прогон CI.
    monkeypatch.setenv("MATRIX_HOMESERVER", "")
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "")
    monkeypatch.setenv("MATRIX_USER_ID", "")
    monkeypatch.setenv("MATRIX_DEVICE_ID", "")
    monkeypatch.setenv("MATRIX_CODE_DEV_ECHO", "1")

    # Тот же админ, что и в test_admin_main (одна БД в CI).
    admin_login = "test_admin@example.com"
    redmine_id = 123
    room_id = "!room123:example.com"

    client.get("/setup", follow_redirects=True)
    csrf = client.cookies.get("admin_csrf")
    client.post(
        "/setup",
        data={
            "login": admin_login,
            "password": "StrongPassword123",
            "password_confirm": "StrongPassword123",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    client.get("/login")
    csrf_login = client.cookies.get("admin_csrf")
    logged = client.post(
        "/login",
        data={"login": admin_login, "password": "StrongPassword123", "csrf_token": csrf_login},
        follow_redirects=False,
    )
    if logged.status_code == 401:
        pytest.skip(
            "Вход тестового admin не удался (в БД другой пароль). "
            "Используйте ту же БД, что в test_admin_main, или чистый инстанс."
        )
    assert logged.status_code in (302, 303), logged.status_code

    client.get("/matrix/bind", follow_redirects=True)
    csrf_bind = client.cookies.get("admin_csrf")
    start = client.post(
        "/matrix/bind/start",
        data={
            "redmine_id": str(redmine_id),
            "room_id": room_id,
            "csrf_token": csrf_bind or "",
        },
        follow_redirects=True,
    )
    assert start.status_code == 200
    m = re.search(r"Dev code:</strong>\s*<code>(\d{6})</code>", start.text)
    assert m, f"Не найден Dev code в ответе: {start.text[:300]}"
    code = m.group(1)

    csrf_confirm = client.cookies.get("admin_csrf")
    confirm = client.post(
        "/matrix/bind/confirm",
        data={
            "redmine_id": str(redmine_id),
            "room_id": room_id,
            "code": code,
            "csrf_token": csrf_confirm or "",
        },
        follow_redirects=False,
    )
    assert confirm.status_code in (303, 302)

    # Проверяем, что bot_users.room обновился. Не asyncio.run + async engine: TestClient
    # крутит ASGI в своём потоке/loop; повторное использование того же AsyncEngine в
    # другом loop даёт зависание asyncpg (полный pytest с test_bot.py перед этим файлом).
    import psycopg

    db_url = os.environ["DATABASE_URL"]
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT room FROM bot_users WHERE redmine_id = %s",
                (redmine_id,),
            )
            row = cur.fetchone()
    assert row is not None
    assert row[0] == room_id

