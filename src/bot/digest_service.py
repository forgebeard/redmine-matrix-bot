"""Слив накопленных digest-строк после снятия DND."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.sender import REDMINE_URL, resolve_room

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


def _event_label(event_type: str) -> str:
    mapping = {
        "comment": "Комментарий",
        "status_change": "Смена статуса",
        "assigned": "Назначение",
        "reassigned": "Переназначение",
        "unassigned": "Снятие исполнителя",
        "watcher_added": "Добавлен наблюдатель",
        "watcher_removed": "Удалён наблюдатель",
        "reminder": "Напоминание",
        "issue_updated": "Обновление",
    }
    return mapping.get(str(event_type or ""), str(event_type or "Обновление"))


def _issue_url(issue_id: int) -> str:
    base = (REDMINE_URL or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/issues/{issue_id}"


def _aggregate_digest_items(rows: list[Any]) -> list[dict[str, Any]]:
    # Spike decision (phase 3): strategy A.
    # Используем только поля pending_digests без миграции схемы и без N+1 к Redmine.
    by_issue: dict[int, dict[str, Any]] = {}
    for r in rows:
        iid = int(r.issue_id)
        item = by_issue.get(iid)
        if item is None:
            item = {
                "issue_id": iid,
                "subject": str(r.issue_subject or ""),
                "events": [],
                "changes": [],
                "comments": [],
                "reminders_count": 0,
                "status_name": "",
                "assigned_to": "",
                "url": _issue_url(iid),
            }
            by_issue[iid] = item
        event_type = str(r.event_type or "")
        if event_type:
            item["events"].append(event_type)
            item["changes"].append(
                {"field": "Событие", "old": "—", "new": _event_label(event_type)}
            )
        if event_type == "reminder":
            item["reminders_count"] = int(item["reminders_count"]) + 1
        notes = str(r.journal_notes or "").strip()
        if notes:
            item["comments"].append(notes)
        status_name = str(r.status_name or "").strip()
        if status_name:
            item["status_name"] = status_name
        assigned_to = str(r.assigned_to or "").strip()
        if assigned_to:
            item["assigned_to"] = assigned_to
    for item in by_issue.values():
        item["extra_changes"] = max(0, len(item["changes"]) - 6)
        item["changes"] = item["changes"][:6]
    return list(by_issue.values())


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
        row_ids: list[int] = []
        for r in rows:
            row_ids.append(int(r.id))
        items = _aggregate_digest_items(rows)
        content: dict[str, Any] = {}
        try:
            html, plain_tpl = await render_named_template(
                session,
                "tpl_digest",
                {
                    "items": items,
                    "digest_items": items,
                },
            )
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
