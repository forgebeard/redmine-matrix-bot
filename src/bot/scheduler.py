"""Планировщик: периодические задачи бота.

check_all_users, daily_report, cleanup_state_files.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nio import AsyncClient
    from redminelib import Redmine

logger = logging.getLogger("redmine_bot")
GLOBAL_UNASSIGNED_NEW_STATE_UID = 0


def _safe_html_or_empty(value: str) -> str:
    from utils import safe_html

    return safe_html(value or "")


def build_daily_report_template_context(
    *,
    report_date: str,
    total_open: int,
    info_count: int,
    overdue_count: int,
    info_items_html: str,
    overdue_items_html: str,
) -> dict[str, Any]:
    """Контекст Jinja для ``tpl_daily_report`` (Matrix утренний отчёт).

    Поля совпадают с переменными в ``templates/bot/tpl_daily_report.html.j2``.
    """
    return {
        "report_date": report_date,
        "total_open": total_open,
        "info_count": info_count,
        "overdue_count": overdue_count,
        "info_items_html": info_items_html,
        "overdue_items_html": overdue_items_html,
    }


async def check_all_users(
    client: AsyncClient,
    redmine: Redmine,
    *,
    now_tz: Callable[[], datetime],
    check_interval: int,
    runtime_status_file: Path,
    bot_instance_id,
    bot_lease_ttl: int,
    redmine_client_for_user: Callable[[Redmine, dict[str, Any]], Redmine],
    last_check_time: dict[int, datetime],
    max_concurrent: int = 5,
) -> None:
    """Проверка задач ВСЕХ пользователей. Параллельно по max_concurrent."""

    from bot.config_hot_reload import refresh_runtime_lists_from_db
    from bot.journal_tick import run_journal_tick
    from bot.sender import reset_dm_failed
    from database.session import get_session_factory

    start = time.monotonic()
    reset_dm_failed()
    logger.info("🔍 Проверка в %s...", now_tz().strftime("%H:%M:%S"))

    session_factory = get_session_factory()
    await refresh_runtime_lists_from_db(session_factory)

    try:
        await run_journal_tick(client, redmine, now_tz=now_tz)
    except Exception as e:
        logger.error("❌ journal_tick: %s", e, exc_info=True)
    elapsed = time.monotonic() - start
    try:
        runtime_status_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_cycle_at": now_tz().isoformat(),
            "last_cycle_duration_s": round(elapsed, 3),
            "error_count": 0,
            "journal_engine": "v5_only",
        }
        runtime_status_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("Не удалось обновить runtime_status.json", exc_info=True)
    logger.info("✅ Журнальный цикл завершён за %.1fс", elapsed)
    return


def _issue_is_unassigned(issue: Any) -> bool:
    try:
        assignee = getattr(issue, "assigned_to", None)
    except Exception:
        assignee = None
    if assignee is None:
        return True
    try:
        assignee_id = getattr(assignee, "id", None)
        return assignee_id in (None, "", 0)
    except Exception:
        return False


def _redmine_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def check_unassigned_new_issues(
    client: AsyncClient,
    redmine: Redmine,
    *,
    now_tz: Callable[[], datetime],
    last_check_time: dict[str, datetime],
    bot_instance_id,
    bot_lease_ttl: int,
) -> None:
    """Checks NEW unassigned issues and routes by status/version/priority."""
    from bot.async_utils import run_in_thread
    from bot.config_hot_reload import refresh_runtime_lists_from_db
    from bot.config_state import CATALOGS, GROUPS
    from bot.logic import STATUS_NEW, issue_matches_cfg, should_notify
    from bot.sender import send_safe
    from database.session import get_session_factory
    from database.state_repo import (
        load_user_issue_state,
        try_acquire_user_lease,
        upsert_user_issue_state,
    )
    from preferences import can_notify

    await refresh_runtime_lists_from_db(get_session_factory())

    state_key = "unassigned_new"
    prev_check = last_check_time.get(state_key)
    warm_start_from = datetime.now(UTC) - timedelta(minutes=15)
    status_new_id = CATALOGS.status_name_to_id.get(STATUS_NEW) if CATALOGS else None
    params: dict[str, Any] = {"assigned_to_id": "!*"}
    if status_new_id is not None:
        params["status_id"] = str(status_new_id)
    else:
        params["status_id"] = "open"
    updated_from = prev_check or warm_start_from
    params["updated_on"] = f">={_redmine_ts(updated_from)}"

    logger.info("🧭 Unassigned NEW: старт проверки")
    try:
        issues = await run_in_thread(lambda: list(redmine.issue.filter(**params)))
    except Exception as primary_err:
        logger.warning(
            "⚠ check_unassigned_new: primary filter failed (%s), fallback to open issues scan",
            primary_err,
        )
        fallback_params: dict[str, Any] = {
            "status_id": "open",
            "updated_on": f">={_redmine_ts(updated_from)}",
        }
        try:
            issues = await run_in_thread(lambda: list(redmine.issue.filter(**fallback_params)))
        except Exception as fallback_err:
            logger.error(
                "❌ check_unassigned_new: Redmine fallback failed: %s",
                fallback_err,
                exc_info=True,
            )
            return

    candidate_issues = [
        issue
        for issue in issues
        if getattr(getattr(issue, "status", None), "name", "") == STATUS_NEW
        and _issue_is_unassigned(issue)
    ]
    if prev_check:
        logger.info(
            "🧭 Unassigned NEW: %d задач (обновлено с %s)",
            len(candidate_issues),
            prev_check.strftime("%H:%M:%S"),
        )
    else:
        logger.info(
            "🧭 Unassigned NEW: %d задач (тёплый старт с %s)",
            len(candidate_issues),
            warm_start_from.strftime("%H:%M:%S"),
        )

    session_factory = get_session_factory()
    lease_until = datetime.now(UTC) + timedelta(seconds=bot_lease_ttl)
    now = now_tz()

    async with session_factory() as session:
        acquired = await try_acquire_user_lease(
            session,
            GLOBAL_UNASSIGNED_NEW_STATE_UID,
            lease_owner_id=bot_instance_id,
            lease_until=lease_until,
        )
        if not acquired:
            return
        await session.commit()

        sent, reminders, overdue, journals = await load_user_issue_state(
            session, GLOBAL_UNASSIGNED_NEW_STATE_UID
        )
        changed_sent: set[str] = set()

        for issue in candidate_issues:
            iid = str(issue.id)
            if iid in sent:
                continue

            recipients: list[tuple[dict[str, Any], str]] = []
            matched_group_rooms = 0
            for group_cfg in GROUPS:
                room = (group_cfg.get("room") or "").strip()
                if not room or not should_notify(group_cfg, "new"):
                    continue
                if not issue_matches_cfg(issue, group_cfg):
                    continue
                recipients.append((group_cfg, room))
                matched_group_rooms += 1

            logger.info(
                "🧭 Unassigned NEW #%s: matched groups=%d",
                issue.id,
                matched_group_rooms,
            )

            sent_rooms: set[str] = set()
            for cfg, room in recipients:
                if room in sent_rooms:
                    continue
                try:
                    if can_notify(cfg, priority="", context="group_room"):
                        await send_safe(
                            client,
                            issue,
                            cfg,
                            room,
                            "new",
                            db_session=session,
                        )
                        sent_rooms.add(room)
                except Exception as e:
                    logger.error(
                        "❌ Unassigned NEW send failed #%s → %s: %s",
                        issue.id,
                        room[:20],
                        e,
                    )

            sent[iid] = {"notified_at": now.isoformat(), "status": STATUS_NEW}
            changed_sent.add(iid)

        await upsert_user_issue_state(
            session,
            GLOBAL_UNASSIGNED_NEW_STATE_UID,
            changed_sent,
            sent,
            reminders,
            overdue,
            journals,
        )
        await session.commit()

    last_check_time[state_key] = datetime.now(UTC)
    logger.info("🧭 Unassigned NEW: завершено, новых отправок: %d", len(changed_sent))


async def daily_report(
    client: AsyncClient,
    redmine: Redmine,
    *,
    now_tz: Callable[[], datetime],
    today_tz: Callable[[], datetime],
    redmine_client_for_user: Callable[[Redmine, dict[str, Any]], Redmine],
    redmine_url: str,
) -> None:
    """Утренний отчёт (09:00) — каждому пользователю с notify=all."""
    from bot.config_hot_reload import refresh_runtime_lists_from_db
    from bot.config_state import USERS
    from bot.logic import STATUS_INFO_PROVIDED, plural_days, should_notify
    from bot.sender import _strip_html_to_plain, resolve_room
    from bot.template_loader import render_named_template
    from database.session import get_session_factory
    from matrix_send import room_send_with_retry
    from preferences import can_notify

    session_factory = get_session_factory()
    await refresh_runtime_lists_from_db(session_factory)

    logger.info("📊 Утренний отчёт...")

    async with session_factory() as db_session:
        for user_cfg in USERS:
            if not should_notify(user_cfg, "all"):
                continue
            if not can_notify(user_cfg, priority="", context="personal"):
                logger.debug(
                    "Утренний отчёт: пропуск (время/DND), user %s",
                    user_cfg.get("redmine_id"),
                )
                continue

            uid = user_cfg["redmine_id"]
            room_raw = user_cfg["room"]
            rm_user = redmine_client_for_user(redmine, user_cfg)

            # Резолвим MXID → room_id через кеш (чтобы не создавать дубликат DM)
            try:
                room = await resolve_room(client, room_raw)
            except Exception as e:
                logger.error(
                    "❌ Не удалось резолвить комнату для user %s (%s): %s",
                    uid,
                    room_raw,
                    e,
                )
                continue

            try:
                issues = list(rm_user.issue.filter(assigned_to_id=uid, status_id="open"))
            except Exception as e:
                logger.error("❌ Redmine (%s, user %s): %s", "утренний отчёт", uid, e, exc_info=True)
                continue

            today = today_tz()
            info_provided = [i for i in issues if i.status.name == STATUS_INFO_PROVIDED]
            overdue = sorted(
                [i for i in issues if i.due_date and i.due_date < today], key=lambda i: i.due_date
            )

            info_items_html = ""
            if info_provided:
                info_items_html = "<ul>"
                for i in info_provided[:10]:
                    info_items_html += (
                        f'<li><a href="{redmine_url}/issues/{i.id}">#{i.id}</a> '
                        f"— {_safe_html_or_empty(i.subject)}</li>"
                    )
                info_items_html += "</ul>"
                if len(info_provided) > 10:
                    info_items_html += f"<p><em>...и ещё {len(info_provided) - 10}</em></p>"

            overdue_items_html = ""
            if overdue:
                overdue_items_html = "<ul>"
                for i in overdue[:10]:
                    days = (today - i.due_date).days
                    overdue_items_html += (
                        f'<li><a href="{redmine_url}/issues/{i.id}">#{i.id}</a> '
                        f"— {_safe_html_or_empty(i.subject)} ({plural_days(days)})</li>"
                    )
                overdue_items_html += "</ul>"

            ctx = build_daily_report_template_context(
                report_date=today.strftime("%d.%m.%Y"),
                total_open=len(issues),
                info_count=len(info_provided),
                overdue_count=len(overdue),
                info_items_html=info_items_html,
                overdue_items_html=overdue_items_html,
            )
            try:
                html, plain_opt = await render_named_template(
                    db_session, "tpl_daily_report", ctx
                )
            except Exception as e:
                logger.error(
                    "❌ Рендер tpl_daily_report для user %s: %s", uid, e, exc_info=True
                )
                continue
            plain = (plain_opt or "").strip() or _strip_html_to_plain(html)

            try:
                await room_send_with_retry(
                    client,
                    room,
                    {
                        "msgtype": "m.text",
                        "body": plain,
                        "format": "org.matrix.custom.html",
                        "formatted_body": html,
                    },
                )
                logger.info("📊 Отчёт user %s: %d задач", uid, len(issues))
            except Exception as e:
                logger.error("❌ Отправка отчёта user %s: %s", uid, e)


async def cleanup_state_files(
    redmine: Redmine,
    *,
    now_tz: Callable[[], datetime],
    redmine_client_for_user: Callable[[Redmine, dict[str, Any]], Redmine],
) -> None:
    """Очистка state в Postgres для закрытых задач (03:00)."""
    from bot.config_hot_reload import refresh_runtime_lists_from_db
    from bot.config_state import USERS
    from database.session import get_session_factory
    from database.state_repo import delete_state_rows_not_in_open

    session_factory = get_session_factory()
    await refresh_runtime_lists_from_db(session_factory)

    logger.info("🧹 Очистка state в Postgres для закрытых задач (03:00)...")

    async with session_factory() as session:
        for user_cfg in USERS:
            uid = user_cfg["redmine_id"]
            rm_user = redmine_client_for_user(redmine, user_cfg)
            try:
                open_issues = list(rm_user.issue.filter(assigned_to_id=uid, status_id="open"))
            except Exception as e:
                logger.error(
                    "❌ Redmine (%s, user %s): %s", "очистка state (db)", uid, e, exc_info=True
                )
                continue

            open_ids = {str(i.id) for i in open_issues}
            try:
                await delete_state_rows_not_in_open(session, uid, open_ids)
            except Exception as e:
                logger.error("❌ DB cleanup user %s: %s", uid, e, exc_info=True)

        await session.commit()

    logger.info("🧹 Очистка state в Postgres завершена")


async def retry_dlq_notifications(
    client: AsyncClient,
    *,
    now_tz: Callable[[], datetime],
    batch_limit: int | None = None,
) -> int:
    """Повторная отправка уведомлений из dead-letter queue.

    Записи журнала с ``payload.needs_rerender`` снова рендерятся из шаблона в БД,
    затем отправляется готовое Matrix-тело. Остальные записи — как раньше
    (payload уже ``m.room.message``).

    Возвращает количество успешно доставленных уведомлений.
    """
    from bot.sender import resolve_room
    from bot.template_loader import render_named_template
    from database.dlq_repo import (
        MAX_DLQ_RETRIES,
        dequeue_due_notifications,
        mark_failed,
        mark_sent,
    )
    from database.session import get_session_factory
    from matrix_send import room_send_with_retry

    session_factory = get_session_factory()
    processed = 0

    async with session_factory() as session:
        due = await dequeue_due_notifications(session, limit=batch_limit)
        if not due:
            return 0

        logger.info("🔄 DLQ retry: %d уведомлений готово к отправке", len(due))

        for notif in due:
            try:
                p = notif.payload if isinstance(notif.payload, dict) else {}
                if p.get("needs_rerender"):
                    tpl = str(p.get("template_name") or "tpl_task_change")
                    ctx = p.get("jinja_context") if isinstance(p.get("jinja_context"), dict) else {}
                    plain = str(p.get("plain_body") or f"#{notif.issue_id}")
                    html, plain_tpl = await render_named_template(session, tpl, ctx)
                    matrix_plain = plain_tpl if plain_tpl is not None else plain
                    content = {
                        "msgtype": "m.text",
                        "body": matrix_plain,
                        "format": "org.matrix.custom.html",
                        "formatted_body": html,
                    }
                else:
                    content = notif.payload
                resolved = await resolve_room(client, notif.room_id)
                await room_send_with_retry(client, resolved, content, txn_id=f"dlq_{int(notif.id)}")
                await mark_sent(session, notif.id)
                processed += 1
                logger.info(
                    "✅ DLQ retry #%s → %s (попытка %d/%d)",
                    notif.issue_id,
                    notif.room_id[:20],
                    notif.retry_count,
                    MAX_DLQ_RETRIES,
                )
            except Exception as e:
                await mark_failed(session, notif.id, str(e))
                logger.warning(
                    "⚠ DLQ retry #%s failed (попытка %d/%d): %s",
                    notif.issue_id,
                    notif.retry_count + 1,
                    MAX_DLQ_RETRIES,
                    e,
                )

        await session.commit()

    logger.info("✅ DLQ retry завершена: %d/%d успешно", processed, len(due))
    return processed
