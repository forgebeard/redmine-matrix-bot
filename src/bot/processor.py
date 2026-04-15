"""Обработка задач одного пользователя.

check_user_issues — ядро бота: загрузка из Redmine, детекция изменений,
уведомления, сохранение state в Postgres.
"""

from __future__ import annotations

import logging
from bot.async_utils import run_in_thread
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from redminelib.exceptions import AuthError, BaseRedmineError, ForbiddenError

if TYPE_CHECKING:
    from nio import AsyncClient
    from redminelib import Redmine
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("redmine_bot")


async def check_user_issues(
    client: AsyncClient,
    redmine: Redmine,
    user_cfg: dict[str, Any],
    db_session: AsyncSession,
    *,
    now_tz: Callable[[], datetime],
    today_tz: Callable[[], datetime],
    ensure_tz: Callable[[datetime], datetime],
    last_check_time: dict[int, datetime],
) -> None:
    """Проверяет все открытые задачи одного пользователя.

    Определяет что изменилось и рассылает уведомления.
    Инкрементальная загрузка: после первого цикла запрашиваем только
    задачи, обновлённые с момента последнего успешного цикла.
    """
    from bot.logic import (
        STATUS_NEW,
        STATUS_RV,
        describe_journal,
        detect_new_journals,
        detect_status_change,
        should_notify,
    )
    from bot.sender import send_safe
    from database.state_repo import load_user_issue_state

    uid = user_cfg["redmine_id"]
    room = user_cfg["room"]

    # ── Загружаем задачи из Redmine (инкрементально, в thread pool) ──
    last_check = last_check_time.get(uid)
    try:
        params: dict = {
            "assigned_to_id": uid,
            "status_id": "open",
            "include": ["journals"],
        }
        if last_check:
            params["updated_on"] = f">={last_check.isoformat()}"

        # redminelib — синхронная, выносим в thread чтобы не блокировать event loop
        issues = await run_in_thread(lambda: list(redmine.issue.filter(**params)))

        if last_check:
            logger.info(
                "👤 User %s: %d задач (обновлено с %s)",
                uid,
                len(issues),
                last_check.strftime("%H:%M:%S"),
            )
        else:
            logger.info("👤 User %s: %d задач (полная загрузка)", uid, len(issues))
    except Exception as e:
        _log_redmine_list_error(uid, e, "загрузка задач")
        return

    # ── Загружаем state (Postgres) ──
    sent, reminders, overdue_n, journals = await load_user_issue_state(db_session, uid)

    # Наборы для upsert в DB
    changed_sent: set[str] = set()
    changed_reminders: set[str] = set()
    changed_overdue: set[str] = set()
    changed_journals: set[str] = set()

    now = now_tz()
    today = now.date()

    # Импорт helpers из bot.logic
    from bot.config_state import STATUS_ROOM_MAP, USERS, VERSION_ROOM_MAP
    from bot.logic import (
        STATUS_INFO_PROVIDED,
        STATUS_REOPENED,
        _group_room,
    )
    from bot.logic import (
        _group_member_rooms as _group_member_rooms_raw,
    )
    from bot.logic import (
        get_extra_rooms_for_new as _get_extra_rooms_for_new_raw,
    )
    from bot.logic import (
        get_extra_rooms_for_rv as _get_extra_rooms_for_rv_raw,
    )

    for issue in issues:
        iid = str(issue.id)

        # Локальные wrapper'ы для доступа к глобальным картам
        def _group_member_rooms(user_cfg: dict) -> set[str]:
            return _group_member_rooms_raw(user_cfg, USERS)

        def _get_extra_rooms_for_new(issue, user_cfg: dict) -> set[str]:
            return _get_extra_rooms_for_new_raw(issue, user_cfg, VERSION_ROOM_MAP, USERS)

        def _get_extra_rooms_for_rv(issue, user_cfg: dict) -> set[str]:
            return _get_extra_rooms_for_rv_raw(
                issue, user_cfg, STATUS_ROOM_MAP, VERSION_ROOM_MAP, USERS
            )

        try:
            # ═══ 1. СМЕНА СТАТУСА ═══
            old_status = detect_status_change(issue, sent)
            if old_status:
                if should_notify(user_cfg, "status_change"):
                    extra = (
                        f"Статус: <strong>{_safe_html(old_status)}</strong> "
                        f"→ <strong>{_safe_html(issue.status.name)}</strong>"
                    )
                    await send_safe(
                        client, issue, user_cfg, room, "status_change", extra_text=extra
                    )
                sent[iid]["status"] = issue.status.name
                changed_sent.add(iid)

            # ═══ 2. НОВАЯ ЗАДАЧА ═══
            if issue.status.name == STATUS_NEW and iid not in sent:
                if should_notify(user_cfg, "new"):
                    await send_safe(client, issue, user_cfg, room, "new")
                    for personal_room in _group_member_rooms(user_cfg):
                        if personal_room != room:
                            await send_safe(client, issue, user_cfg, personal_room, "new")
                    group_room = _group_room(user_cfg)
                    if group_room and should_notify(_cfg_for_room(user_cfg, group_room), "new"):
                        await send_safe(client, issue, user_cfg, group_room, "new")
                    for extra_room in _get_extra_rooms_for_new(issue, user_cfg):
                        await send_safe(client, issue, user_cfg, extra_room, "new")
                sent[iid] = {
                    "notified_at": now.isoformat(),
                    "status": STATUS_NEW,
                    "group_last_notified_at": now.isoformat(),
                }
                changed_sent.add(iid)

            # ═══ 3. ПЕРЕДАНО В РАБОТУ.РВ ═══
            elif issue.status.name == STATUS_RV and iid not in sent:
                if should_notify(user_cfg, "new"):
                    await send_safe(client, issue, user_cfg, room, "new")
                    for personal_room in _group_member_rooms(user_cfg):
                        if personal_room != room:
                            await send_safe(client, issue, user_cfg, personal_room, "new")
                    group_room = _group_room(user_cfg)
                    if group_room and should_notify(_cfg_for_room(user_cfg, group_room), "new"):
                        await send_safe(client, issue, user_cfg, group_room, "new")
                    for extra_room in _get_extra_rooms_for_rv(issue, user_cfg):
                        await send_safe(client, issue, user_cfg, extra_room, "new")
                sent[iid] = {
                    "notified_at": now.isoformat(),
                    "status": STATUS_RV,
                    "group_last_notified_at": now.isoformat(),
                }
                changed_sent.add(iid)
            elif issue.status.name in (STATUS_NEW, STATUS_RV) and iid in sent:
                group_room = _group_room(user_cfg)
                if group_room:
                    last_group = sent.get(iid, {}).get("group_last_notified_at")
                    if last_group:
                        elapsed_group = (
                            now - ensure_tz(datetime.fromisoformat(last_group))
                        ).total_seconds()
                    else:
                        elapsed_group = GROUP_REPEAT_SECONDS + 1
                    if elapsed_group >= GROUP_REPEAT_SECONDS and should_notify(
                        _cfg_for_room(user_cfg, group_room), "new"
                    ):
                        await send_safe(client, issue, user_cfg, group_room, "new")
                        sent[iid]["group_last_notified_at"] = now.isoformat()
                        changed_sent.add(iid)

            # ═══ 4. ИНФОРМАЦИЯ ПРЕДОСТАВЛЕНА ═══
            elif issue.status.name == STATUS_INFO_PROVIDED:
                if iid not in sent:
                    if should_notify(user_cfg, "info"):
                        await send_safe(client, issue, user_cfg, room, "info")
                    sent[iid] = {
                        "notified_at": now.isoformat(),
                        "status": STATUS_INFO_PROVIDED,
                    }
                    changed_sent.add(iid)
                else:
                    # Напоминание каждый час
                    if should_notify(user_cfg, "reminder"):
                        last_rem = reminders.get(iid, {}).get("last_reminder")
                        if last_rem:
                            time_since = (
                                now - ensure_tz(datetime.fromisoformat(last_rem))
                            ).total_seconds()
                        else:
                            notified_at = ensure_tz(
                                datetime.fromisoformat(sent[iid]["notified_at"])
                            )
                            time_since = (now - notified_at).total_seconds()

                        if time_since >= REMINDER_AFTER:
                            await send_safe(client, issue, user_cfg, room, "reminder")
                            reminders[iid] = {"last_reminder": now.isoformat()}
                            changed_reminders.add(iid)

            # ═══ 5. ОТКРЫТО ПОВТОРНО ═══
            elif issue.status.name == STATUS_REOPENED and iid not in sent:
                if should_notify(user_cfg, "reopened"):
                    await send_safe(client, issue, user_cfg, room, "reopened")
                sent[iid] = {
                    "notified_at": now.isoformat(),
                    "status": STATUS_REOPENED,
                }
                changed_sent.add(iid)

            # ═══ 6. ПРОЧИЕ СТАТУСЫ — первое обнаружение (тихо) ═══
            elif iid not in sent:
                sent[iid] = {
                    "notified_at": now.isoformat(),
                    "status": issue.status.name,
                }
                changed_sent.add(iid)

            # ═══ 7. ПРОСРОЧЕННЫЕ ЗАДАЧИ ═══
            if issue.due_date and issue.due_date < today:
                if should_notify(user_cfg, "overdue"):
                    last_n = overdue_n.get(iid, {}).get("last_notified")
                    if not last_n or ensure_tz(datetime.fromisoformat(last_n)).date() < today:
                        await send_safe(client, issue, user_cfg, room, "overdue")
                        overdue_n[iid] = {"last_notified": now.isoformat()}
                        changed_overdue.add(iid)

            # ═══ 8. ЖУРНАЛЫ: КОММЕНТАРИИ И ИЗМЕНЕНИЯ ПОЛЕЙ ═══
            new_jrnls, max_id = detect_new_journals(issue, journals)

            # Защита от спама старыми журналами
            if iid not in journals:
                if max_id > 0:
                    journals[iid] = {"last_journal_id": max_id}
                    changed_journals.add(iid)
                    logger.debug("📝 #%s: инициализация journal_id=%s (пропуск)", iid, max_id)
            elif new_jrnls and iid in sent and should_notify(user_cfg, "issue_updated"):
                _skip_st = old_status is not None
                descs = [
                    d for d in (describe_journal(j, skip_status=_skip_st) for j in new_jrnls) if d
                ]
                if descs:
                    tail = descs[-5:]
                    combined = "<br/>".join(_safe_html(d) for d in tail)
                    if len(descs) > 5:
                        combined = f"<em>...и ещё {len(descs) - 5}</em><br/>" + combined
                    await send_safe(
                        client, issue, user_cfg, room, "issue_updated", extra_text=combined
                    )

                if max_id > journals.get(iid, {}).get("last_journal_id", 0):
                    journals[iid] = {"last_journal_id": max_id}
                    changed_journals.add(iid)
            else:
                if max_id > journals.get(iid, {}).get("last_journal_id", 0):
                    journals[iid] = {"last_journal_id": max_id}
                    changed_journals.add(iid)

        except Exception as e:
            logger.error("❌ Ошибка обработки #%s (user %s): %s", issue.id, uid, e, exc_info=True)
            continue

    # ── Сохраняем state (Postgres) ──
    from database.state_repo import upsert_user_issue_state

    issue_ids_changed = changed_sent | changed_reminders | changed_overdue | changed_journals
    if issue_ids_changed:
        await upsert_user_issue_state(
            db_session,
            uid,
            issue_ids_changed,
            sent,
            reminders,
            overdue_n,
            journals,
        )


# ── Helpers ──────────────────────────────────────────────────────────────────


# Константы задаются из main.py через _init_processor_config
# Значения по умолчанию — из config.py
def _get_group_repeat_seconds() -> int:
    try:
        from config import GROUP_REPEAT_SECONDS

        return GROUP_REPEAT_SECONDS
    except Exception:
        return 1800


def _get_reminder_after() -> int:
    try:
        from config import REMINDER_AFTER

        return REMINDER_AFTER
    except Exception:
        return 3600


GROUP_REPEAT_SECONDS: int = _get_group_repeat_seconds()
REMINDER_AFTER: int = _get_reminder_after()


def _cfg_for_room(user_cfg: dict[str, Any], room_id: str) -> dict[str, Any]:
    from bot.logic import _cfg_for_room as _raw

    return _raw(user_cfg, room_id)


def _safe_html(value: str) -> str:
    from utils import safe_html

    return safe_html(value)


def _log_redmine_list_error(uid: int, err: Exception, where: str) -> None:
    if isinstance(err, (AuthError, ForbiddenError)):
        logger.error("❌ Redmine доступ (%s, user %s): %s", where, uid, err)
    elif isinstance(err, BaseRedmineError):
        logger.error("❌ Redmine API (%s, user %s): %s", where, uid, err)
    else:
        logger.error("❌ Redmine (%s, user %s): %s", where, uid, err, exc_info=True)
