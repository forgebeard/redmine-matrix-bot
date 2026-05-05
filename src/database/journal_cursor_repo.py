"""Курсор журналов по задаче: ``bot_issue_journal_cursor``."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BotIssueJournalCursor


async def get_last_journal_id(session: AsyncSession, issue_id: int) -> int:
    row = await session.scalar(
        select(BotIssueJournalCursor.last_journal_id).where(
            BotIssueJournalCursor.issue_id == issue_id
        )
    )
    return int(row or 0)


async def upsert_last_journal_id(
    session: AsyncSession, issue_id: int, last_journal_id: int
) -> None:
    now = datetime.now(UTC)
    stmt = pg_insert(BotIssueJournalCursor).values(
        issue_id=issue_id,
        last_journal_id=last_journal_id,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[BotIssueJournalCursor.issue_id],
        set_={
            "last_journal_id": last_journal_id,
            "updated_at": now,
        },
    )
    await session.execute(stmt)
