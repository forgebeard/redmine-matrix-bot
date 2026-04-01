"""Запись операций в `bot_ops_audit` и структурированный лог."""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from admin.runtime import logger
from admin.timeutil import now_utc
from database.models import BotOpsAudit


async def audit_op(
    session: AsyncSession,
    action: str,
    status: str,
    actor_email: str | None = None,
    detail: str | None = None,
) -> None:
    row = BotOpsAudit(
        actor_email=(actor_email or "").strip().lower() or None,
        action=action,
        status=status,
        detail=(detail or "")[:2000] or None,
    )
    session.add(row)
    logger.info(
        json.dumps(
            {
                "level": "AUDIT",
                "action": action,
                "status": status,
                "actor": actor_email or "",
                "detail": detail or "",
                "ts": now_utc().isoformat(),
            },
            ensure_ascii=False,
        )
    )
