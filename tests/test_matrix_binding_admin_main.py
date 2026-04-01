import os
import re

import pytest
from fastapi.testclient import TestClient

import admin_main
os.environ.setdefault("APP_MASTER_KEY", "0123456789abcdef0123456789abcdef")


def _db_ready() -> bool:
    db_url = os.getenv("DATABASE_URL", "")
    return bool(db_url) and db_url.startswith("postgresql://")


@pytest.fixture
def client():
    return TestClient(admin_main.app)


def test_matrix_bind_redirects_to_login_without_auth(client: TestClient):
    r = client.get("/matrix/bind", follow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    assert r.headers.get("location", "").endswith("/login")


@pytest.mark.skipif(not _db_ready(), reason="DB auth требует DATABASE_URL (postgresql://...)")
def test_matrix_bind_flow_dev_echo_updates_bot_user_room(client: TestClient):
    os.environ["MATRIX_CODE_DEV_ECHO"] = "1"

    admin_login = "matrix_user"
    redmine_id = 123
    room_id = "!room123:example.com"

    from tests.support_admin import ensure_admin_logged_in

    ensure_admin_logged_in(
        client,
        final_login=admin_login,
        final_password="StrongPassword123",
    )

    bind_page = client.get("/matrix/bind")
    csrf_bind = bind_page.cookies.get("admin_csrf")
    assert csrf_bind, "ожидается CSRF cookie после открытия /matrix/bind"

    start = client.post(
        "/matrix/bind/start",
        data={
            "redmine_id": str(redmine_id),
            "room_id": room_id,
            "csrf_token": csrf_bind,
        },
        follow_redirects=True,
    )
    assert start.status_code == 200
    m = re.search(r"Dev code:</b>\s*(\d{6})", start.text)
    assert m, f"Не найден Dev code в ответе: {start.text[:300]}"
    code = m.group(1)

    csrf_confirm = client.cookies.get("admin_csrf") or csrf_bind
    confirm = client.post(
        "/matrix/bind/confirm",
        data={
            "redmine_id": str(redmine_id),
            "room_id": room_id,
            "code": code,
            "csrf_token": csrf_confirm,
        },
        follow_redirects=False,
    )
    assert confirm.status_code in (303, 302)

    # Проверяем, что bot_users.room обновился.
    from database.session import get_session_factory
    from sqlalchemy import select
    from database.models import BotUser

    factory = get_session_factory()
    async def _check():
        async with factory() as session:
            r = await session.execute(select(BotUser).where(BotUser.redmine_id == redmine_id))
            return r.scalar_one_or_none()

    import asyncio

    user_row = asyncio.run(_check())
    assert user_row is not None
    assert user_row.room == room_id

