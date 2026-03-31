#!/usr/bin/env python3
"""Emergency script: reset admin password directly in DB."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

from sqlalchemy import delete, select

from database.models import BotAppUser, BotSession
from database.session import get_session_factory
from security import hash_password, validate_password_policy


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", help="Admin login")
    parser.add_argument("--email", help="Deprecated alias for --login")
    parser.add_argument("--password", required=True, help="New password")
    args = parser.parse_args()

    login = (args.login or args.email or "").strip().lower()
    if not login:
        print("Provide --login (or legacy --email)", file=sys.stderr)
        return 2
    password = args.password
    ok, reason = validate_password_policy(password, email=login)
    if not ok:
        print(f"Password policy failed: {reason}", file=sys.stderr)
        return 3

    factory = get_session_factory()
    async with factory() as session:
        r = await session.execute(select(BotAppUser).where(BotAppUser.email == login))
        user = r.scalar_one_or_none()
        if not user:
            print("User not found", file=sys.stderr)
            return 4
        if user.role != "admin":
            print("User exists but is not admin", file=sys.stderr)
            return 5
        user.password_hash = hash_password(password)
        user.session_version = (user.session_version or 1) + 1
        await session.execute(delete(BotSession).where(BotSession.user_id == user.id))
        await session.commit()
    print("Admin password reset completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
