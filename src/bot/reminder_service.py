"""Напоминания по застою для журнального движка v2 (поля ``bot_issue_state``)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.journal_handlers import journal_render_send_or_dlq
from bot.journal_pipeline import reload_issue_with_journals
from bot.logic import _cfg_for_room, issue_matches_cfg, should_notify
from bot.routing import get_matching_route
from bot.template_context import build_issue_context
from database.digest_repo import insert_digest
from database.models import BotIssueState
from preferences import can_notify

logger = logging.getLogger("redmine_bot")


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def update_reminder_timers(
    session: AsyncSession,
    issue: Any,
    *,
    catalogs: Any,
    now: datetime,
) -> None:
    """Один раз на кандидата после цикла журналов: таймеры по ``issue.status.is_closed`` (план §3)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    issue_id = int(issue.id)
    now_u = _utc(now)
    is_closed = bool(getattr(getattr(issue, "status", None), "is_closed", False))

    if is_closed:
        await session.execute(
            update(BotIssueState)
            .where(BotIssueState.issue_id == issue_id)
            .values(
                group_reminder_due_at=None,
                personal_reminder_due_at=None,
                reminder_count=0,
            )
        )
        return

    interval_sec = max(60, int(catalogs.cycle_int("DEFAULT_REMINDER_INTERVAL", 14400)))
    due = now_u + timedelta(seconds=interval_sec)

    await session.execute(
        update(BotIssueState)
        .where(BotIssueState.issue_id == issue_id)
        .values(
            group_reminder_due_at=due,
            personal_reminder_due_at=due,
            reminder_count=0,
        )
    )

    try:
        aid = int(getattr(getattr(issue, "assigned_to", None), "id", 0) or 0)
    except Exception:
        aid = 0
    if not aid:
        return

    stmt = (
        pg_insert(BotIssueState)
        .values(
            user_redmine_id=aid,
            issue_id=issue_id,
            group_reminder_due_at=due,
            personal_reminder_due_at=due,
            reminder_count=0,
        )
        .on_conflict_do_update(
            index_elements=[BotIssueState.user_redmine_id, BotIssueState.issue_id],
            set_={
                "group_reminder_due_at": due,
                "personal_reminder_due_at": due,
                "reminder_count": 0,
            },
        )
    )
    await session.execute(stmt)


def _user_cfg_by_redmine(users: list[dict[str, Any]], redmine_id: int) -> dict[str, Any] | None:
    for u in users:
        if int(u.get("redmine_id") or -1) == int(redmine_id):
            return u
    return None


async def process_reminders(
    client: Any,
    redmine: Any,
    session: AsyncSession,
    *,
    catalogs: Any,
    users: list[dict[str, Any]],
    routes_cfg: dict[str, Any] | None,
    groups: list[dict[str, Any]],
    now_tz: Callable[[], datetime],
) -> int:
    """Строки с прошедшим ``*_reminder_due_at`` и ``reminder_count < MAX_REMINDERS`` → Matrix / digest / DLQ."""
    now_u = _utc(now_tz())
    max_rem = max(1, int(catalogs.cycle_int("MAX_REMINDERS", 3)))
    interval_sec = max(60, int(catalogs.cycle_int("DEFAULT_REMINDER_INTERVAL", 14400)))

    stmt = (
        select(BotIssueState)
        .where(
            BotIssueState.reminder_count < max_rem,
            or_(
                and_(
                    BotIssueState.group_reminder_due_at.isnot(None),
                    BotIssueState.group_reminder_due_at <= now_u,
                ),
                and_(
                    BotIssueState.personal_reminder_due_at.isnot(None),
                    BotIssueState.personal_reminder_due_at <= now_u,
                ),
            ),
        )
        .order_by(BotIssueState.issue_id, BotIssueState.user_redmine_id)
        .limit(100)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    sent = 0

    for st in rows:
        cfg = _user_cfg_by_redmine(users, int(st.user_redmine_id))
        if not cfg:
            continue
        try:
            issue = await reload_issue_with_journals(redmine, int(st.issue_id))
        except Exception as e:
            logger.warning("reminder_reload_issue #%s: %s", st.issue_id, e)
            continue

        try:
            cur_assignee = int(getattr(getattr(issue, "assigned_to", None), "id", 0) or 0)
        except Exception:
            cur_assignee = 0
        if cur_assignee != int(st.user_redmine_id):
            await session.execute(
                update(BotIssueState)
                .where(
                    BotIssueState.user_redmine_id == st.user_redmine_id,
                    BotIssueState.issue_id == st.issue_id,
                )
                .values(
                    group_reminder_due_at=None,
                    personal_reminder_due_at=None,
                )
            )
            continue

        if bool(getattr(getattr(issue, "status", None), "is_closed", False)):
            st.group_reminder_due_at = None
            st.personal_reminder_due_at = None
            st.reminder_count = 0
            continue

        # tpl_reminder: полный issue-контекст + поля напоминания (не путать с tpl_digest — отдельная модель).
        base_ctx = build_issue_context(
            issue,
            catalogs,
            reminder_text="Задача без движения",
            title="Напоминание",
            emoji="⏰",
        )
        plain = f"#{issue.id} {base_ctx['subject']}: напоминание"

        group_due = st.group_reminder_due_at is not None and st.group_reminder_due_at <= now_u
        personal_due = st.personal_reminder_due_at is not None and st.personal_reminder_due_at <= now_u

        matched = get_matching_route(issue, routes_cfg, cfg, groups=groups)
        if group_due and matched and matched.room_id.strip():
            gcfg = _cfg_for_room(cfg, matched.room_id)
            if issue_matches_cfg(issue, gcfg) and should_notify(gcfg, "reminder"):
                if can_notify(gcfg, priority=str(getattr(issue.priority, "name", "") or "")):
                    await journal_render_send_or_dlq(
                        client,
                        session,
                        room_id=matched.room_id,
                        template_name="tpl_reminder",
                        jinja_context=base_ctx,
                        plain_body=plain,
                        user_redmine_id=int(cfg.get("redmine_id") or 0),
                        issue_id=int(issue.id),
                        notification_type="reminder",
                    )
                    sent += 1

        room = (cfg.get("room") or "").strip()
        if personal_due and room and issue_matches_cfg(issue, cfg):
            if should_notify(cfg, "reminder") and can_notify(
                cfg, priority=str(getattr(issue.priority, "name", "") or "")
            ):
                await journal_render_send_or_dlq(
                    client,
                    session,
                    room_id=room,
                    template_name="tpl_reminder",
                    jinja_context=dict(base_ctx),
                    plain_body=plain,
                    user_redmine_id=int(cfg.get("redmine_id") or 0),
                    issue_id=int(issue.id),
                    notification_type="reminder",
                )
                sent += 1
            else:
                await insert_digest(
                    session,
                    user_id=int(cfg["id"]),
                    issue_id=int(issue.id),
                    issue_subject=str(issue.subject or "")[:255],
                    event_type="reminder",
                    journal_id=None,
                    journal_notes=None,
                    status_name=str(getattr(issue.status, "name", None) or ""),
                    assigned_to=str(getattr(getattr(issue, "assigned_to", None), "name", "") or ""),
                )

        st.reminder_count = int(st.reminder_count or 0) + 1
        if st.reminder_count >= max_rem:
            st.group_reminder_due_at = None
            st.personal_reminder_due_at = None
        else:
            nxt = now_u + timedelta(seconds=interval_sec)
            st.group_reminder_due_at = nxt
            st.personal_reminder_due_at = nxt

    return sent
