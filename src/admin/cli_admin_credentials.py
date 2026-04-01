"""CLI-операции: смена пароля и логина админа (вызываются из scripts/*)."""

from __future__ import annotations

import getpass
import sys

from sqlalchemy import delete, select

from admin.auth_helpers import normalize_admin_login, validate_new_login_shape
from database.models import BotAppUser, BotSession
from database.session import get_session_factory
from security import hash_password, validate_password_policy


async def async_reset_password(login: str, password: str | None, *, force: bool) -> int:
    """
    password=None или '' — запрос пароля через getpass (+ повтор, если не force).
    Иначе используется переданная строка.
    """
    login_n = normalize_admin_login(login)
    if not login_n:
        print("Укажите непустой логин", file=sys.stderr)
        return 1

    pwd = (password or "").strip() or None
    if pwd is None:
        pwd = getpass.getpass("Новый пароль: ")
        if not force:
            confirm = getpass.getpass("Повтор пароля: ")
            if confirm != pwd:
                print("Пароли не совпадают", file=sys.stderr)
                return 1

    ok, reason = validate_password_policy(pwd, login=login_n)
    if not ok:
        print(reason or "Политика пароля", file=sys.stderr)
        return 2

    factory = get_session_factory()
    async with factory() as session:
        r = await session.execute(select(BotAppUser).where(BotAppUser.login == login_n))
        user = r.scalar_one_or_none()
        if not user or user.role != "admin":
            print("Администратор с таким логином не найден", file=sys.stderr)
            return 3
        user.password_hash = hash_password(pwd)
        user.must_change_credentials = False
        user.session_version = (user.session_version or 1) + 1
        await session.execute(delete(BotSession).where(BotSession.user_id == user.id))
        await session.commit()
    print("Пароль обновлён, все сессии сброшены.")
    return 0


async def async_change_login(old_login: str, new_login: str) -> int:
    old_n = normalize_admin_login(old_login)
    new_n = normalize_admin_login(new_login)
    if not old_n or not new_n:
        print("Логины не должны быть пустыми", file=sys.stderr)
        return 1
    ok, reason = validate_new_login_shape(new_n)
    if not ok:
        print(reason or "Некорректный новый логин", file=sys.stderr)
        return 2

    factory = get_session_factory()
    async with factory() as session:
        r = await session.execute(select(BotAppUser).where(BotAppUser.login == old_n))
        user = r.scalar_one_or_none()
        if not user or user.role != "admin":
            print("Администратор со старым логином не найден", file=sys.stderr)
            return 3
        taken = await session.execute(select(BotAppUser.id).where(BotAppUser.login == new_n))
        if taken.scalar_one_or_none() is not None:
            print("Новый логин уже занят", file=sys.stderr)
            return 4
        user.login = new_n
        user.must_change_credentials = False
        user.session_version = (user.session_version or 1) + 1
        await session.execute(delete(BotSession).where(BotSession.user_id == user.id))
        await session.commit()
    print("Логин изменён, все сессии сброшены.")
    return 0
