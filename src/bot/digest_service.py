"""Слив накопленных digest-строк после снятия DND."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.sender import resolve_room

# tpl_digest — отдельная модель контекста (только ``items``), не ``build_issue_context``.
from bot.template_loader import render_named_template
from database.digest_repo import (
    delete_digest_rows,
    list_digest_rows_for_users,
    user_ids_having_digests,
)
from database.dlq_repo import enqueue_notification
from matrix_send import room_send_with_retry

logger = logging.getLogger("redmine_bot")

_TAG_RE = re.compile(r"<[^>]+>")


def _plain_from_html(html: str) -> str:
    t = _TAG_RE.sub("", html or "")
    return " ".join(t.split()).strip() or "Дайджест"


async def drain_pending_digests(
    client: Any,
    session: AsyncSession,
    *,
    users_by_bot_id: dict[int, dict[str, Any]],
    drain_max_users: int,
) -> int:
    """
    В начале тика: до ``drain_max_users`` пользователей без DND с непустым ``pending_digests``.
    Возвращает число успешно отправленных дайджестов.
    """
    uids = await user_ids_having_digests(session, drain_max_users)
    sent = 0
    for bot_uid in uids:
        cfg = users_by_bot_id.get(bot_uid)
        if not cfg:
            continue
        rows = await list_digest_rows_for_users(session, [bot_uid])
        if not rows:
            continue
        items: list[dict[str, Any]] = []
        row_ids: list[int] = []
        by_issue: dict[int, dict[str, Any]] = {}
        for r in rows:
            row_ids.append(int(r.id))
            iid = int(r.issue_id)
            if iid not in by_issue:
                by_issue[iid] = {
                    "issue_id": iid,
                    "subject": r.issue_subject,
                    "events": [],
                }
            by_issue[iid]["events"].append(str(r.event_type))
        items = list(by_issue.values())
        content: dict[str, Any] = {}
        try:
            html, plain_tpl = await render_named_template(session, "tpl_digest", {"items": items})
            plain = plain_tpl if plain_tpl is not None else _plain_from_html(html)
            content = {
                "msgtype": "m.text",
                "body": plain,
                "format": "org.matrix.custom.html",
                "formatted_body": html,
            }
            room = (cfg.get("room") or "").strip()
            if not room:
                continue
            resolved = await resolve_room(client, room)
            await room_send_with_retry(client, resolved, content)
            await delete_digest_rows(session, row_ids)
            await session.commit()
            sent += 1
        except Exception as e:
            logger.warning("digest_drain_failed user_bot_id=%s: %s", bot_uid, e)
            await session.rollback()
            try:
                await enqueue_notification(
                    session,
                    user_redmine_id=int(cfg.get("redmine_id") or 0),
                    issue_id=int(items[0]["issue_id"]) if items else 0,
                    room_id=(cfg.get("room") or "").strip(),
                    notification_type="digest",
                    payload=content,
                    error=str(e),
                )
                await session.commit()
            except Exception:
                await session.rollback()
    return sent
