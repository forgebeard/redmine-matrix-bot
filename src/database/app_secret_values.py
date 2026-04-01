"""Чтение расшифрованных значений из ``app_secrets`` (интеграции)."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AppSecret
from security import decrypt_secret, load_master_key

logger = logging.getLogger("redmine_bot")


async def load_decrypted_secrets(session: AsyncSession, names: Iterable[str]) -> dict[str, str]:
    """Возвращает словарь имя → значение; отсутствующие ключи не попадают в результат."""
    name_list = [n.strip() for n in names if (n or "").strip()]
    if not name_list:
        return {}
    key = load_master_key()
    r = await session.execute(select(AppSecret).where(AppSecret.name.in_(name_list)))
    rows = list(r.scalars().all())
    out: dict[str, str] = {}
    for row in rows:
        try:
            out[row.name] = decrypt_secret(row.ciphertext, row.nonce, key)
        except Exception:
            logger.warning("Не удалось расшифровать секрет name=%s", row.name, exc_info=True)
            out[row.name] = ""
    return out


def merge_secret(db_map: dict[str, str], name: str, env_value: str | None) -> str:
    """Приоритет: значение из БД по ключу ``name``, иначе переменная окружения."""
    v = (db_map.get(name) or "").strip()
    if v:
        return v
    return (env_value or "").strip()
