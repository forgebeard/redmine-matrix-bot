"""Очередь дайджестов при DND: ``pending_digests``."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BotUser, PendingDigest


async def insert_digest(
    session: AsyncSession,
    *,
    user_id: int,
    issue_id: int,
    issue_subject: str,
    event_type: str,
    journal_id: int | None,
    journal_notes: str | None,
    status_name: str | None,
    assigned_to: str | None,
) -> None:
    row = PendingDigest(
        user_id=user_id,
        issue_id=issue_id,
        issue_subject=issue_subject[:255],
        event_type=event_type,
        journal_id=journal_id,
        journal_notes=journal_notes,
        status_name=status_name,
        assigned_to=assigned_to,
    )
    session.add(row)


async def list_digest_rows_for_users(
    session: AsyncSession,
    user_ids: list[int],
) -> list[PendingDigest]:
    if not user_ids:
        return []
    result = await session.execute(select(PendingDigest).where(PendingDigest.user_id.in_(user_ids)))
    return list(result.scalars().all())


async def delete_digest_rows(session: AsyncSession, ids: list[int]) -> None:
    if not ids:
        return
    await session.execute(delete(PendingDigest).where(PendingDigest.id.in_(ids)))


async def user_ids_having_digests(session: AsyncSession, limit: int) -> list[int]:
    """Первые ``limit`` ``bot_users.id`` с digest и ``dnd=false``."""
    stmt = (
        select(PendingDigest.user_id)
        .join(BotUser, PendingDigest.user_id == BotUser.id)
        .where(BotUser.dnd.is_(False))
        .distinct()
        .limit(limit)
    )
    r = await session.execute(stmt)
    return [int(x[0]) for x in r.all()]
