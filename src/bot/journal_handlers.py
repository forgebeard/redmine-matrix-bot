"""Обработка одной записи журнала: групповой и персональный поток, DLQ, digest."""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config_state import CATALOGS
from bot.logic import _cfg_for_room, describe_journal, issue_matches_cfg, should_notify
from bot.routing import get_matching_route
from bot.sender import resolve_room
from bot.template_context import build_issue_context
from bot.template_loader import render_named_template
from database.digest_repo import insert_digest
from database.dlq_repo import enqueue_notification
from matrix_send import room_send_with_retry
from preferences import can_notify

logger = logging.getLogger("redmine_bot")


def jinja_context_json_safe(ctx: dict[str, Any]) -> dict[str, Any]:
    """Контекст для DLQ / retry: JSON-serializable; при сбое — shallow-sanitize (страховка)."""
    try:
        json.dumps(ctx, ensure_ascii=False)
        return dict(ctx)
    except (TypeError, ValueError) as e:
        logger.warning("jinja_context not JSON-safe, sanitizing: %s", e)
        out: dict[str, Any] = {}
        for k, v in ctx.items():
            key = str(k)
            if v is None or isinstance(v, (str, int, float, bool)):
                out[key] = v
            elif isinstance(v, dict):
                out[key] = jinja_context_json_safe(v)
            elif isinstance(v, list):
                out[key] = [_json_safe_scalar(x) for x in v]
            else:
                out[key] = str(v)
        return out


def _json_safe_scalar(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return jinja_context_json_safe(v)
    if isinstance(v, list):
        return [_json_safe_scalar(x) for x in v]
    return str(v)


def assert_json_serializable_payload(payload: dict[str, Any]) -> None:
    json.dumps(payload)


def _normalize_detail_prop(d: dict[str, Any]) -> str:
    return str(d.get("name") or d.get("property") or "").strip()


def infer_event_type(journal: Any) -> str:
    has_notes = bool(getattr(journal, "notes", None) and str(journal.notes).strip())
    if has_notes:
        return "comment"
    try:
        for d in journal.details or []:
            prop = _normalize_detail_prop(d)
            if prop in ("assigned_to_id", "assigned_to"):
                return "assigned"
            if prop == "status_id":
                return "status_change"
    except Exception:
        pass
    return "issue_updated"


def former_assignee_redmine_id(journal: Any) -> int | None:
    """Из journal.details для смены исполнителя; пустой old_value = нет «бывшего» (план §4)."""
    try:
        details = journal.details or []
    except Exception:
        return None
    for d in details:
        if not isinstance(d, dict):
            continue
        prop = _normalize_detail_prop(d)
        if prop not in ("assigned_to_id", "assigned_to"):
            continue
        if "old_value" not in d:
            continue
        # Redmine REST API returns old_value/new_value as strings, even for numeric IDs.
        # "" and "0" both mean "was unassigned" (no former assignee).
        old = d.get("old_value")
        if old is None:
            continue
        s = str(old).strip()
        if s in ("", "0"):
            continue
        try:
            rid = int(s)
        except ValueError:
            continue
        if rid > 0:
            return rid
    return None


def user_cfg_by_redmine_id(users: list[dict[str, Any]], redmine_id: int) -> dict[str, Any] | None:
    for u in users:
        if int(u.get("redmine_id") or -1) == int(redmine_id):
            return u
    return None


async def personal_recipient_cfgs(
    session: AsyncSession,
    issue: Any,
    journal: Any,
    assignee_cfg: dict[str, Any],
    users: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Исполнитель, бывший исполнитель (если есть), наблюдатели из кэша; дедуп; без self-personal."""
    from database.watcher_cache_repo import list_bot_user_ids_for_issue

    try:
        author_rid = int(getattr(getattr(journal, "user", None), "id", 0) or 0)
    except Exception:
        author_rid = 0

    by_bot_id = {int(u["id"]): u for u in users if u.get("id") is not None}
    out: list[dict[str, Any]] = []
    seen_bot: set[int] = set()

    def append_cfg(cfg: dict[str, Any]) -> None:
        bid = int(cfg.get("id") or 0)
        rid = int(cfg.get("redmine_id") or 0)
        if not bid or not rid:
            return
        if rid == author_rid:
            return
        if bid in seen_bot:
            return
        seen_bot.add(bid)
        out.append(cfg)

    append_cfg(assignee_cfg)

    try:
        assignee_rid = int(assignee_cfg.get("redmine_id") or 0)
    except Exception:
        assignee_rid = 0

    former_rid = former_assignee_redmine_id(journal)
    if former_rid and former_rid != assignee_rid:
        fc = user_cfg_by_redmine_id(users, former_rid)
        if fc:
            append_cfg(fc)

    try:
        iid = int(issue.id)
    except Exception:
        iid = 0
    if iid:
        for bot_uid in await list_bot_user_ids_for_issue(session, iid):
            wcfg = by_bot_id.get(int(bot_uid))
            if wcfg:
                append_cfg(wcfg)

    return out


async def journal_render_send_or_dlq(
    client: Any,
    session: AsyncSession,
    *,
    room_id: str,
    template_name: str,
    jinja_context: dict[str, Any],
    plain_body: str,
    user_redmine_id: int,
    issue_id: int,
    notification_type: str,
) -> None:
    """Рендер Jinja → Matrix; при любой ошибке — DLQ, без raise (курсор журнала вперёд).

    Ошибка до готового Matrix-тела: payload с ``needs_rerender`` и JSON-safe контекстом (A1).
    Ошибка после рендера: payload = готовое тело Matrix (повтор без рендера).
    """
    content: dict[str, Any] | None = None
    try:
        html, plain_tpl = await render_named_template(session, template_name, jinja_context)
        matrix_plain = plain_tpl if plain_tpl is not None else plain_body
        content = {
            "msgtype": "m.text",
            "body": matrix_plain,
            "format": "org.matrix.custom.html",
            "formatted_body": html,
        }
        resolved = await resolve_room(client, room_id)
        await room_send_with_retry(client, resolved, content)
    except Exception as e:
        err = str(e)
        if content is not None:
            try:
                assert_json_serializable_payload(content)
                await enqueue_notification(
                    session,
                    user_redmine_id=user_redmine_id,
                    issue_id=issue_id,
                    room_id=room_id,
                    notification_type=notification_type,
                    payload=content,
                    error=err,
                )
            except Exception as dlq_e:
                logger.error("journal_dlq_enqueue_failed #%s: %s", issue_id, dlq_e, exc_info=True)
        else:
            dlq_payload = {
                "needs_rerender": True,
                "template_name": template_name,
                "jinja_context": jinja_context_json_safe(jinja_context),
                "plain_body": plain_body,
                "issue_id": int(issue_id),
                "room_id": room_id,
                "notification_type": notification_type,
            }
            try:
                assert_json_serializable_payload(dlq_payload)
                await enqueue_notification(
                    session,
                    user_redmine_id=user_redmine_id,
                    issue_id=issue_id,
                    room_id=room_id,
                    notification_type=notification_type,
                    payload=dlq_payload,
                    error=err,
                )
            except Exception as dlq_e:
                logger.error("journal_dlq_enqueue_failed #%s: %s", issue_id, dlq_e, exc_info=True)
        logger.warning(
            "journal_notify_dlq issue_id=%s room=%s type=%s: %s",
            issue_id,
            (room_id or "")[:32],
            notification_type,
            err,
            exc_info=True,
        )


async def handle_journal_entry(
    client: Any,
    session: AsyncSession,
    *,
    issue: Any,
    journal: Any,
    assignee_cfg: dict[str, Any],
    routes_cfg: dict[str, Any] | None,
    groups: list[dict[str, Any]],
    users: list[dict[str, Any]],
) -> None:
    """Групповая комната по маршруту + личные уведомления получателям (или digest при DND)."""
    cats = CATALOGS
    event_type = infer_event_type(journal)
    extra = describe_journal(journal, skip_status=False, catalogs=cats) or ""

    base_ctx = build_issue_context(
        issue,
        cats,
        event_type=event_type,
        extra_text=extra,
        title="Обновление задачи",
        emoji="📝",
    )

    try:
        tpl_name = "tpl_new_issue" if len(list(getattr(issue, "journals", None) or [])) == 1 else "tpl_task_change"
    except Exception:
        tpl_name = "tpl_task_change"

    matched = get_matching_route(issue, routes_cfg, assignee_cfg, groups=groups)
    if matched and matched.room_id.strip():
        skip_group = event_type == "assigned" and not matched.notify_on_assignment
        if not skip_group:
            gcfg = _cfg_for_room(assignee_cfg, matched.room_id)
            if issue_matches_cfg(issue, gcfg) and should_notify(gcfg, "issue_updated"):
                plain = f"#{issue.id} {base_ctx['subject']}: {event_type}"
                if can_notify(gcfg, priority=str(getattr(issue.priority, "name", "") or "")):
                    await journal_render_send_or_dlq(
                        client,
                        session,
                        room_id=matched.room_id,
                        template_name=tpl_name,
                        jinja_context=base_ctx,
                        plain_body=plain,
                        user_redmine_id=int(assignee_cfg.get("redmine_id") or 0),
                        issue_id=int(issue.id),
                        notification_type="issue_updated",
                    )

    recipients = await personal_recipient_cfgs(session, issue, journal, assignee_cfg, users)
    for rcfg in recipients:
        room = (rcfg.get("room") or "").strip()
        if not room or not issue_matches_cfg(issue, rcfg):
            continue
        pctx = dict(base_ctx)
        plain_p = f"#{issue.id} {pctx['subject']}: {event_type}"
        try:
            if should_notify(rcfg, "issue_updated") and can_notify(
                rcfg,
                priority=str(getattr(issue.priority, "name", "") or ""),
            ):
                await journal_render_send_or_dlq(
                    client,
                    session,
                    room_id=room,
                    template_name=tpl_name,
                    jinja_context=pctx,
                    plain_body=plain_p,
                    user_redmine_id=int(rcfg.get("redmine_id") or 0),
                    issue_id=int(issue.id),
                    notification_type="issue_updated",
                )
            else:
                await insert_digest(
                    session,
                    user_id=int(rcfg["id"]),
                    issue_id=int(issue.id),
                    issue_subject=str(issue.subject or "")[:255],
                    event_type=event_type,
                    journal_id=int(journal.id),
                    journal_notes=getattr(journal, "notes", None),
                    status_name=str(getattr(issue.status, "name", None) or ""),
                    assigned_to=str(getattr(getattr(issue, "assigned_to", None), "name", "") or ""),
                )
        except Exception:
            logger.debug("journal_personal_digest_failed", exc_info=True)
