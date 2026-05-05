"""Один тик журнального движка v2: digest → фаза A/B → handlers → DLQ."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.catalogs import load_catalogs
from bot.config_state import GROUPS, ROUTING, USERS
from bot.digest_service import drain_pending_digests
from bot.journal_handlers import handle_journal_entry
from bot.journal_pipeline import (
    advance_cursor_after_journal,
    aggregate_journals_first_old_last_new,
    iter_new_journals_for_issue,
    load_bot_user_redmine_ids,
    persist_watermark,
    phase_a_candidates,
    reload_issue_with_journals,
    sync_watcher_cache_for_issue,
)
from bot.reminder_service import process_reminders, update_reminder_timers
from bot.scheduler import retry_dlq_notifications
from database.models import BotUser
from database.session import get_session_factory
from database.watcher_cache_repo import (
    delete_stale_watcher_rows,
    issue_ids_watched_by_bot_users,
)

logger = logging.getLogger("redmine_bot")

_TICK_COUNTER = 0


def _assignee_cfg(issue: Any, users: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        aid = int(getattr(getattr(issue, "assigned_to", None), "id", 0) or 0)
    except Exception:
        return None
    if not aid:
        return None
    for u in users:
        if int(u.get("redmine_id") or -1) == aid:
            return u
    return None


async def _redmine_id_to_bot_id_map(session: AsyncSession) -> dict[int, int]:
    r = await session.execute(select(BotUser.redmine_id, BotUser.id))
    return {int(red): int(bid) for red, bid in r.all()}


def _users_by_bot_id(users: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(u["id"]): u for u in users if u.get("id") is not None}


async def run_journal_tick(
    client: Any,
    redmine: Any,
    *,
    now_tz: Callable[[], Any],
) -> None:
    """Точка входа планировщика для основного тика назначенных задач и напоминаний."""
    global _TICK_COUNTER
    _TICK_COUNTER += 1

    session_factory = get_session_factory()
    dlq_batch = 10
    async with session_factory() as session:
        catalogs = await load_catalogs(session)
        from bot import config_state as _cs

        _cs.CATALOGS = catalogs

        max_issues = catalogs.cycle_int("MAX_ISSUES_PER_TICK", 50)
        max_pages = catalogs.cycle_int("MAX_PAGES_PER_TICK", 3)
        drain_max = catalogs.cycle_int("DRAIN_MAX_USERS_PER_TICK", 5)
        n_refresh = catalogs.cycle_int("WATCHER_CACHE_REFRESH_EVERY_N_TICKS", 10)
        dlq_batch = max(1, catalogs.cycle_int("DLQ_BATCH_SIZE", 10))

        users = list(USERS)
        ubid = _users_by_bot_id(users)
        await drain_pending_digests(
            client, session, users_by_bot_id=ubid, drain_max_users=drain_max
        )

        bot_ids = await load_bot_user_redmine_ids(session)
        watched = await issue_ids_watched_by_bot_users(session)
        routes_cfg = ROUTING or {}
        groups = list(GROUPS)

        candidates, max_on = await phase_a_candidates(
            redmine,
            session,
            bot_user_redmine_ids=bot_ids,
            watched_issue_ids=watched,
            max_issues=max_issues,
            max_pages=max_pages,
        )
        await persist_watermark(session, max_on)
        await session.commit()

        rid_map = await _redmine_id_to_bot_id_map(session)

        if n_refresh > 0 and _TICK_COUNTER % n_refresh == 0 and watched:
            for wid in list(watched)[: max_issues * max_pages]:
                try:
                    iss = await reload_issue_with_journals(redmine, int(wid))
                    await sync_watcher_cache_for_issue(session, iss, redmine_id_to_bot_id=rid_map)
                except Exception as e:
                    logger.debug("watcher_refresh_issue %s: %s", wid, e)
            await session.commit()
            check_interval = catalogs.cycle_int("CHECK_INTERVAL", 90)
            stale_sec = max(24 * 3600, 2 * max(1, n_refresh) * check_interval)
            stale_before = datetime.now(UTC) - timedelta(seconds=stale_sec)
            try:
                await delete_stale_watcher_rows(
                    session,
                    list(watched)[: max_issues * max_pages],
                    updated_before=stale_before,
                )
            except Exception as e:
                logger.debug("watcher_delete_stale: %s", e)
            await session.commit()

        for iss in candidates:
            try:
                full = await reload_issue_with_journals(redmine, int(iss.id))
            except Exception as e:
                logger.warning("journal_reload_issue #%s: %s", getattr(iss, "id", "?"), e)
                continue
            await sync_watcher_cache_for_issue(session, full, redmine_id_to_bot_id=rid_map)
            new_js = await iter_new_journals_for_issue(session, full)
            assignee = _assignee_cfg(full, users)
            aggregated = aggregate_journals_first_old_last_new(new_js)
            if aggregated is not None:
                if assignee is not None:
                    try:
                        await handle_journal_entry(
                            client,
                            session,
                            issue=full,
                            journal=aggregated,
                            assignee_cfg=assignee,
                            routes_cfg=routes_cfg,
                            groups=groups,
                            users=users,
                        )
                    except Exception:
                        logger.error(
                            "journal_handle_failed #%s j=%s",
                            full.id,
                            getattr(aggregated, "id", "?"),
                            exc_info=True,
                        )
                else:
                    logger.info(
                        "journal_skip_no_assignee issue_id=%s journal_id=%s",
                        full.id,
                        getattr(aggregated, "id", "?"),
                    )
                await advance_cursor_after_journal(session, int(full.id), int(aggregated.id))
                await session.commit()

            try:
                issue_for_timers = await reload_issue_with_journals(redmine, int(full.id))
            except Exception as e:
                logger.warning("journal_reload_timers issue #%s: %s", full.id, e)
                issue_for_timers = full
            await update_reminder_timers(
                session,
                issue_for_timers,
                catalogs=catalogs,
                now=now_tz(),
            )
            await session.commit()

        await process_reminders(
            client,
            redmine,
            session,
            catalogs=catalogs,
            users=users,
            routes_cfg=routes_cfg,
            groups=groups,
            now_tz=now_tz,
        )
        await session.commit()

    try:
        await retry_dlq_notifications(client, now_tz=now_tz, batch_limit=dlq_batch)
    except Exception as e:
        logger.warning("journal_tick_dlq_retry: %s", e)
