"""Runtime API for thin bot worker: pull commands and report delivery status."""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.dlq_repo import MAX_DLQ_RETRIES
from database.dlq_repo import mark_failed, mark_sent
from database.models import PendingNotification
from database.session import get_session

router = APIRouter(tags=["bot-runtime"])
COMMAND_LEASE_SECONDS = 60


@router.get("/api/bot/commands", response_class=JSONResponse)
async def bot_pull_commands(
    limit: int = Query(20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """Return due notifications as delivery commands for thin bot worker."""
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=COMMAND_LEASE_SECONDS)
    stmt = (
        select(PendingNotification)
        .where(
            PendingNotification.next_retry_at.isnot(None),
            PendingNotification.next_retry_at <= now,
            PendingNotification.retry_count < MAX_DLQ_RETRIES,
        )
        .order_by(PendingNotification.next_retry_at.asc(), PendingNotification.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    for row in rows:
        # Reserve command for one worker for a short lease window.
        row.next_retry_at = lease_until
    await session.commit()

    commands = [
        {
            "command_id": row.id,
            "schema_version": "v1",
            "kind": "send_matrix_message",
            "room_id": row.room_id,
            "notification_type": row.notification_type,
            "payload": row.payload,
            "retry_count": row.retry_count,
            "issue_id": row.issue_id,
            "user_redmine_id": row.user_redmine_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "lease_until": lease_until.isoformat(),
        }
        for row in rows
    ]
    return {"ok": True, "commands": commands}


@router.post("/api/bot/commands/{command_id}/ack", response_class=JSONResponse)
async def bot_command_ack(
    command_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Mark command as delivered and remove it from queue."""
    await mark_sent(session, command_id)
    await session.commit()
    return {"ok": True}


@router.post("/api/bot/commands/{command_id}/error", response_class=JSONResponse)
async def bot_command_error(
    command_id: int,
    error: str = Query(..., min_length=1, max_length=4000),
    session: AsyncSession = Depends(get_session),
):
    """Report failed delivery and schedule retry."""
    row = await mark_failed(session, command_id, error)
    await session.commit()
    if row is None:
        return JSONResponse({"ok": False, "error": "command_not_found"}, status_code=404)
    return {"ok": True, "retry_count": row.retry_count}
