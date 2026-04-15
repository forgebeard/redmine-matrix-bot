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
    check_user_issues_fn: Callable[..., Any],
    last_check_time: dict[int, datetime],
    max_concurrent: int = 5,
) -> None:
    """Проверка задач ВСЕХ пользователей. Параллельно по max_concurrent."""
    import asyncio

    from bot.config_state import USERS
    from bot.sender import reset_dm_failed
    from database.session import get_session_factory
    from database.state_repo import try_acquire_user_lease

    start = time.monotonic()
    reset_dm_failed()
    logger.info("🔍 Проверка в %s...", now_tz().strftime("%H:%M:%S"))

    session_factory = get_session_factory()
    lease_owner_id = bot_instance_id
    lease_ttl = bot_lease_ttl
    error_count = 0
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _process_user(user_cfg: dict) -> None:
        nonlocal error_count
        uid = user_cfg.get("redmine_id")
        lease_until = datetime.now(UTC) + timedelta(seconds=lease_ttl)

        async with semaphore:
            async with session_factory() as session:
                try:
                    acquired = await try_acquire_user_lease(
                        session,
                        uid,
                        lease_owner_id=lease_owner_id,
                        lease_until=lease_until,
                    )
                    if not acquired:
                        return

                    await session.commit()
                    rm_user = redmine_client_for_user(redmine, user_cfg)
                    await check_user_issues_fn(
                        client,
                        rm_user,
                        user_cfg,
                        session,
                        now_tz=now_tz,
                        today_tz=lambda: now_tz().date(),
                        ensure_tz=lambda dt: dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt,
                        last_check_time=last_check_time,
                    )
                    await session.commit()
                    last_check_time[uid] = datetime.now(UTC)
                except Exception as e:
                    logger.error("❌ DB-state цикл проверки user %s: %s", uid, e, exc_info=True)
                    error_count += 1
                    try:
                        await session.rollback()
                    except Exception:
                        pass

    tasks = [asyncio.create_task(_process_user(u)) for u in USERS]
    await asyncio.gather(*tasks, return_exceptions=True)

    elapsed = time.monotonic() - start
    try:
        runtime_status_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_cycle_at": now_tz().isoformat(),
            "last_cycle_duration_s": round(elapsed, 3),
            "error_count": int(error_count),
        }
        runtime_status_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("Не удалось обновить runtime_status.json", exc_info=True)

    logger.info("✅ Проверка завершена за %.1fс", elapsed)
    if elapsed > check_interval * 0.8:
        logger.warning(
            "⚠️ Цикл (%dс) > 0.8×интервала (%dс). Увеличьте CHECK_INTERVAL или max_concurrent.",
            int(elapsed),
            check_interval,
        )


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
    from bot.config_state import USERS
    from bot.logic import STATUS_INFO_PROVIDED, plural_days, should_notify
    from bot.sender import resolve_room
    from matrix_send import room_send_with_retry
    from preferences import can_notify
    from utils import safe_html

    logger.info("📊 Утренний отчёт...")

    for user_cfg in USERS:
        if not should_notify(user_cfg, "all"):
            continue
        if not can_notify(user_cfg, priority="", dt=now_tz()):
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
                uid, room_raw, e,
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

        html = f"<h3>📅 Отчёт на {today.strftime('%d.%m.%Y')}</h3>"
        html += f"<p><strong>Открытых задач:</strong> {len(issues)}</p>"
        html += f"<p><strong>«{STATUS_INFO_PROVIDED}»:</strong> {len(info_provided)}</p>"

        if info_provided:
            html += "<ul>"
            for i in info_provided[:10]:
                html += (
                    f'<li><a href="{redmine_url}/issues/{i.id}">#{i.id}</a> '
                    f"— {safe_html(i.subject)}</li>"
                )
            html += "</ul>"
            if len(info_provided) > 10:
                html += f"<p><em>...и ещё {len(info_provided) - 10}</em></p>"

        html += f"<p><strong>Просроченных:</strong> {len(overdue)}</p>"
        if overdue:
            html += "<ul>"
            for i in overdue[:10]:
                days = (today - i.due_date).days
                html += (
                    f'<li><a href="{redmine_url}/issues/{i.id}">#{i.id}</a> '
                    f"— {safe_html(i.subject)} ({plural_days(days)})</li>"
                )
            html += "</ul>"

        plain = (
            f"Отчёт {today.strftime('%d.%m.%Y')}: {len(issues)} задач, {len(overdue)} просрочено"
        )

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
    from bot.config_state import USERS
    from database.session import get_session_factory
    from database.state_repo import delete_state_rows_not_in_open

    logger.info("🧹 Очистка state в Postgres для закрытых задач (03:00)...")
    session_factory = get_session_factory()

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
) -> int:
    """Повторная отправка уведомлений из dead-letter queue.

    Возвращает количество обработанных уведомлений.
    """
    from database.dlq_repo import (
        dequeue_due_notifications,
        mark_failed,
        mark_sent,
    )
    from database.session import get_session_factory
    from matrix_send import room_send_with_retry

    session_factory = get_session_factory()
    processed = 0

    async with session_factory() as session:
        due = await dequeue_due_notifications(session)
        if not due:
            return 0

        logger.info("🔄 DLQ retry: %d уведомлений готово к отправке", len(due))

        for notif in due:
            try:
                await room_send_with_retry(client, notif.room_id, notif.payload)
                await mark_sent(session, notif.id)
                processed += 1
                logger.info(
                    "✅ DLQ retry #%s → %s (попытка %d/%d)",
                    notif.issue_id,
                    notif.room_id[:20],
                    notif.retry_count,
                    5,
                )
            except Exception as e:
                await mark_failed(session, notif.id, str(e))
                logger.warning(
                    "⚠ DLQ retry #%s failed (попытка %d/5): %s",
                    notif.issue_id,
                    notif.retry_count + 1,
                    e,
                )

        await session.commit()

    logger.info("✅ DLQ retry завершена: %d/%d успешно", processed, len(due))
    return processed