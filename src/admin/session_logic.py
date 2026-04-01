"""Запросы к БД для проверки наличия админа и статуса onboarding."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from admin.constants import (
    ONBOARDING_SKIPPED_SECRET,
    REQUIRED_SECRET_NAMES,
    RUNTIME_STATUS_FILE,
)
from admin.runtime import admin_exists_cache, integration_status_cache
from database.models import AppSecret, BotAppUser


def runtime_status_from_file() -> dict:
    p = Path(RUNTIME_STATUS_FILE)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


async def has_admin(session: AsyncSession, use_cache: bool = True) -> bool:
    if use_cache:
        cached = admin_exists_cache.get()
        if cached is not None:
            return cached
    any_admin = await session.execute(
        select(BotAppUser.id).where(BotAppUser.role == "admin").limit(1)
    )
    value = any_admin.scalar_one_or_none() is not None
    admin_exists_cache.set(value)
    return value


async def integration_status(session: AsyncSession, use_cache: bool = True) -> dict:
    if use_cache:
        cached = integration_status_cache.get()
        if cached is not None:
            return cached
    rows = await session.execute(
        select(AppSecret.name).where(
            AppSecret.name.in_(REQUIRED_SECRET_NAMES + [ONBOARDING_SKIPPED_SECRET])
        )
    )
    names = {r[0] for r in rows.all()}
    missing = [name for name in REQUIRED_SECRET_NAMES if name not in names]
    status = {
        "configured": len(missing) == 0,
        "missing": missing,
        "skipped": ONBOARDING_SKIPPED_SECRET in names,
    }
    integration_status_cache.set(status)
    return status
