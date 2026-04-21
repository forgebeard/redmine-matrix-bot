"""Обработка задач одного пользователя.

check_user_issues — ядро бота: загрузка из Redmine, детекция изменений,
уведомления, сохранение state в Postgres.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from redminelib.exceptions import AuthError, BaseRedmineError, ForbiddenError

from bot.async_utils import run_in_thread

if TYPE_CHECKING:
    from nio import AsyncClient
    from redminelib import Redmine
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("redmine_bot")


# ── Формат даты для Redmine API ─────────────────────────────────────────────


def _redmine_ts(dt: datetime) -> str:
    """Конвертирует datetime в строку, которую принимает фильтр updated_on.

    Redmine отвергает offset-формат (+03:00).
    Приводим к UTC и отдаём «YYYY-MM-DDTHH:MM:SSZ».
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    from admin.services.bot_decisions import (
        build_first_notification_actions,
        decide_first_issue_notification,
        decide_info_reminder,
        decide_journal_update,
        decide_overdue,
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
            params["updated_on"] = f">={_redmine_ts(last_check)}"

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
    from bot.config_state import GROUPS, USERS, VERSION_ROOM_MAP
    from bot.logic import (
        STATUS_INFO_PROVIDED,
        STATUS_REOPENED,
        _group_room,
        describe_journal,
        detect_new_journals,
        detect_status_change,
        issue_matches_cfg,
        should_notify,
    )
    from bot.logic import (
        _group_member_rooms as _group_member_rooms_raw,
    )
    from bot.logic import (
        get_extra_rooms_for_new as _get_extra_rooms_for_new_raw,
    )

    for issue in issues:
        iid = str(issue.id)

        # Локальные wrapper'ы для доступа к глобальным картам
        def _group_member_rooms(user_cfg: dict) -> set[str]:
            return _group_member_rooms_raw(user_cfg, USERS)

        def _get_extra_rooms_for_new(issue, user_cfg: dict) -> set[str]:
            return _get_extra_rooms_for_new_raw(issue, user_cfg, VERSION_ROOM_MAP, USERS)

        def _matched_global_group_rooms(issue_obj, source_user_cfg: dict) -> set[str]:
            out: set[str] = set()
            source_group_room = (source_user_cfg.get("group_room") or "").strip()
            for g in GROUPS:
                room_id = (g.get("room") or "").strip()
                if not room_id or room_id == source_group_room:
                    continue
                if issue_matches_cfg(issue_obj, g):
                    out.add(room_id)
            return out

        try:
            # ═══ 1. СМЕНА СТАТУСА ═══
            old_status = detect_status_change(issue, sent)
            if old_status:
                # v5 cutover: статусные апдейты отправляются только через журналный движок.
                sent[iid]["status"] = issue.status.name
                changed_sent.add(iid)

            first_decision = decide_first_issue_notification(
                issue_status_name=issue.status.name,
                already_sent=iid in sent,
                status_reopened=STATUS_REOPENED,
            )
            # ═══ 2/3/5. ПЕРВОЕ УВЕДОМЛЕНИЕ (new/transferred/reopened) ═══
            if first_decision is not None:
                if should_notify(user_cfg, first_decision.notification_kind):
                    if first_decision.notification_kind == "new":
                        group_room = _group_room(user_cfg)
                        group_enabled = bool(
                            group_room and should_notify(_cfg_for_room(user_cfg, group_room), "new")
                        )
                        extra_rooms = _get_extra_rooms_for_new(issue, user_cfg)
                        extra_rooms |= _matched_global_group_rooms(issue, user_cfg)
                        actions = build_first_notification_actions(
                            main_room=room,
                            notification_kind="new",
                            personal_rooms={r for r in _group_member_rooms(user_cfg) if r != room},
                            group_room=group_room,
                            group_enabled=group_enabled,
                            extra_rooms=extra_rooms,
                        )
                    else:
                        actions = build_first_notification_actions(
                            main_room=room,
                            notification_kind=first_decision.notification_kind,
                            personal_rooms=set(),
                            group_room=None,
                            group_enabled=False,
                            extra_rooms=set(),
                        )
                    for action in actions:
                        await send_safe(
                            client,
                            issue,
                            user_cfg,
                            action.room_id,
                            action.notification_kind,
                            db_session=db_session,
                        )
                sent[iid] = {
                    "notified_at": now.isoformat(),
                    "status": first_decision.sent_status,
                }
                if first_decision.set_group_notified:
                    sent[iid]["group_last_notified_at"] = now.isoformat()
                changed_sent.add(iid)
            elif iid in sent and sent.get(iid, {}).get("group_last_notified_at"):
                group_rooms = set()
                group_room = _group_room(user_cfg)
                if group_room:
                    group_rooms.add(group_room)
                group_rooms |= _matched_global_group_rooms(issue, user_cfg)
                if group_rooms:
                    last_group = sent.get(iid, {}).get("group_last_notified_at")
                    if last_group:
                        elapsed_group = (
                            now - ensure_tz(datetime.fromisoformat(last_group))
                        ).total_seconds()
                    else:
                        elapsed_group = GROUP_REPEAT_SECONDS + 1
                    if elapsed_group >= GROUP_REPEAT_SECONDS:
                        for gr in group_rooms:
                            if should_notify(_cfg_for_room(user_cfg, gr), "new"):
                                await send_safe(
                                    client, issue, user_cfg, gr, "new", db_session=db_session
                                )
                        sent[iid]["group_last_notified_at"] = now.isoformat()
                        changed_sent.add(iid)

            # ═══ 4. ИНФОРМАЦИЯ ПРЕДОСТАВЛЕНА ═══
            elif issue.status.name == STATUS_INFO_PROVIDED:
                info_decision = decide_info_reminder(
                    is_info_status=True,
                    already_sent=iid in sent,
                    can_notify_info=should_notify(user_cfg, "info"),
                    can_notify_reminder=should_notify(user_cfg, "reminder"),
                    now=now,
                    reminder_after_seconds=REMINDER_AFTER,
                    last_reminder_iso=reminders.get(iid, {}).get("last_reminder"),
                    sent_notified_at_iso=sent.get(iid, {}).get("notified_at"),
                )
                if info_decision and info_decision.notify_kind:
                    await send_safe(
                        client,
                        issue,
                        user_cfg,
                        room,
                        info_decision.notify_kind,
                        db_session=db_session,
                    )
                if info_decision and info_decision.create_sent_state:
                    sent[iid] = {
                        "notified_at": now.isoformat(),
                        "status": STATUS_INFO_PROVIDED,
                    }
                    changed_sent.add(iid)
                if info_decision and info_decision.update_reminder_state:
                    reminders[iid] = {"last_reminder": now.isoformat()}
                    changed_reminders.add(iid)

            # ═══ 6. ПРОЧИЕ СТАТУСЫ — первое обнаружение (тихо) ═══
            elif iid not in sent:
                sent[iid] = {
                    "notified_at": now.isoformat(),
                    "status": issue.status.name,
                }
                changed_sent.add(iid)

            # ═══ 7. ПРОСРОЧЕННЫЕ ЗАДАЧИ ═══
            overdue_decision = decide_overdue(
                is_overdue=bool(issue.due_date and issue.due_date < today),
                can_notify_overdue=should_notify(user_cfg, "overdue"),
                today_iso=today.isoformat(),
                last_notified_iso=overdue_n.get(iid, {}).get("last_notified"),
            )
            if overdue_decision.should_send:
                await send_safe(
                    client, issue, user_cfg, room, "overdue", db_session=db_session
                )
            if overdue_decision.should_update_state:
                overdue_n[iid] = {"last_notified": now.isoformat()}
                changed_overdue.add(iid)

            # ═══ 8. ЖУРНАЛЫ: КОММЕНТАРИИ И ИЗМЕНЕНИЯ ПОЛЕЙ ═══
            new_jrnls, max_id = detect_new_journals(issue, journals)

            # Защита от спама старыми журналами
            prev_last_journal_id = journals.get(iid, {}).get("last_journal_id", 0)
            had_previous_journal_state = iid in journals
            _skip_st = old_status is not None
            descs = [d for d in (describe_journal(j, skip_status=_skip_st) for j in new_jrnls) if d]
            journal_decision = decide_journal_update(
                had_previous_journal_state=had_previous_journal_state,
                current_max_journal_id=max_id,
                previous_last_journal_id=prev_last_journal_id,
                has_new_journal_descriptions=bool(descs),
                was_issue_previously_notified=iid in sent,
                can_notify_issue_updated=should_notify(user_cfg, "issue_updated"),
            )
            if journal_decision.should_send_update:
                # v5 cutover: issue_updated отправляется только через журналный движок.
                pass
            if journal_decision.should_update_last_seen:
                journals[iid] = {"last_journal_id": max_id}
                changed_journals.add(iid)
                if not had_previous_journal_state and max_id > 0:
                    logger.debug("📝 #%s: инициализация journal_id=%s (пропуск)", iid, max_id)

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
