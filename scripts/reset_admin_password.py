#!/usr/bin/env python3
"""Совместимость: используйте scripts/manage_admin_credentials.py reset-password --login …"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from admin.cli_admin_credentials import async_reset_password  # noqa: E402


async def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Сброс пароля админа (предпочтительно: manage_admin_credentials.py)",
    )
    parser.add_argument("--email", default="", help="Устарело: то же, что --login")
    parser.add_argument("--login", default="", help="Логин администратора (предпочтительно)")
    parser.add_argument("--password", required=True, help="Новый пароль")
    args = parser.parse_args()
    login = (args.login or args.email or "").strip()
    if not login:
        print("Укажите --login или устаревший --email", file=sys.stderr)
        return 1
    return await async_reset_password(login, args.password, force=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
