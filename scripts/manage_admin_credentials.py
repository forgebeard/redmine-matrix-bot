#!/usr/bin/env python3
"""Смена пароля и логина администратора панели только через CLI (без веб-форм)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from admin.cli_admin_credentials import async_change_login, async_reset_password  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Управление учёткой админа панели (CLI)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pw = sub.add_parser("reset-password", help="Сменить пароль")
    p_pw.add_argument("--login", required=True, help="Текущий логин администратора")
    p_pw.add_argument(
        "--password",
        default="",
        help="Новый пароль (если не задан — ввод в консоли)",
    )
    p_pw.add_argument(
        "--force",
        action="store_true",
        help="При вводе с клавиатуры не спрашивать повтор пароля",
    )

    p_ln = sub.add_parser("change-login", help="Сменить логин (пароль не меняется)")
    p_ln.add_argument("--old-login", required=True)
    p_ln.add_argument("--new-login", required=True)

    args = parser.parse_args()
    if args.cmd == "reset-password":
        pwd = (args.password or "").strip() or None
        return asyncio.run(async_reset_password(args.login, pwd, force=args.force))
    return asyncio.run(async_change_login(args.old_login, args.new_login))


if __name__ == "__main__":
    raise SystemExit(main())
