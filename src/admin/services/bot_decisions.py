"""Backend decision helpers for bot notification scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class IssueDecision:
    """Decision for first-notification scenarios."""

    notification_kind: str
    sent_status: str
    set_group_notified: bool = False


@dataclass(frozen=True)
class NotificationAction:
    """Single delivery action for first-notification flow."""

    room_id: str
    notification_kind: str


@dataclass(frozen=True)
class InfoReminderDecision:
    """Decision for info/reminder branch."""

    notify_kind: str | None
    create_sent_state: bool
    update_reminder_state: bool


@dataclass(frozen=True)
class OverdueDecision:
    """Decision for overdue notifications."""

    should_send: bool
    should_update_state: bool


@dataclass(frozen=True)
class JournalDecision:
    """Decision for issue journal updates."""

    should_send_update: bool
    should_update_last_seen: bool


def decide_first_issue_notification(
    *,
    issue_status_name: str,
    already_sent: bool,
    status_reopened: str,
) -> IssueDecision | None:
    """Return decision for first-time notification event.

    Decision lives in backend-side domain code; bot runtime consumes it where applicable.
    """
    if already_sent:
        return None

    if issue_status_name == status_reopened:
        return IssueDecision(notification_kind="reopened", sent_status=status_reopened)
    return IssueDecision(
        notification_kind="new", sent_status=issue_status_name, set_group_notified=True
    )


def build_first_notification_actions(
    *,
    main_room: str,
    notification_kind: str,
    personal_rooms: set[str],
    group_room: str | None,
    group_enabled: bool,
    extra_rooms: set[str],
) -> list[NotificationAction]:
    """Build deduplicated delivery actions for first-notification event."""
    ordered_rooms: list[str] = []
    seen: set[str] = set()

    def _add_room(room_id: str | None) -> None:
        rid = (room_id or "").strip()
        if not rid or rid in seen:
            return
        seen.add(rid)
        ordered_rooms.append(rid)

    _add_room(main_room)
    for room_id in sorted(personal_rooms):
        _add_room(room_id)
    if group_enabled:
        _add_room(group_room)
    for room_id in sorted(extra_rooms):
        _add_room(room_id)

    return [
        NotificationAction(room_id=rid, notification_kind=notification_kind)
        for rid in ordered_rooms
    ]


def decide_info_reminder(
    *,
    is_info_status: bool,
    already_sent: bool,
    can_notify_info: bool,
    can_notify_reminder: bool,
    now: datetime,
    reminder_after_seconds: int,
    last_reminder_iso: str | None,
    sent_notified_at_iso: str | None,
) -> InfoReminderDecision | None:
    """Return decision for `info` first notify and subsequent reminders."""
    if not is_info_status:
        return None

    if not already_sent:
        return InfoReminderDecision(
            notify_kind="info" if can_notify_info else None,
            create_sent_state=True,
            update_reminder_state=False,
        )

    if not can_notify_reminder:
        return InfoReminderDecision(
            notify_kind=None,
            create_sent_state=False,
            update_reminder_state=False,
        )

    time_since = None
    if last_reminder_iso:
        try:
            time_since = (now - datetime.fromisoformat(last_reminder_iso)).total_seconds()
        except Exception:
            time_since = None
    if time_since is None and sent_notified_at_iso:
        try:
            time_since = (now - datetime.fromisoformat(sent_notified_at_iso)).total_seconds()
        except Exception:
            time_since = None
    if time_since is None:
        time_since = reminder_after_seconds + 1

    if time_since >= reminder_after_seconds:
        return InfoReminderDecision(
            notify_kind="reminder",
            create_sent_state=False,
            update_reminder_state=True,
        )
    return InfoReminderDecision(
        notify_kind=None,
        create_sent_state=False,
        update_reminder_state=False,
    )


def decide_overdue(
    *,
    is_overdue: bool,
    can_notify_overdue: bool,
    today_iso: str,
    last_notified_iso: str | None,
) -> OverdueDecision:
    """Return decision for overdue branch (at most one notify per day)."""
    if not is_overdue or not can_notify_overdue:
        return OverdueDecision(should_send=False, should_update_state=False)

    if not last_notified_iso:
        return OverdueDecision(should_send=True, should_update_state=True)

    try:
        last_day = datetime.fromisoformat(last_notified_iso).date().isoformat()
    except Exception:
        return OverdueDecision(should_send=True, should_update_state=True)

    if last_day < today_iso:
        return OverdueDecision(should_send=True, should_update_state=True)
    return OverdueDecision(should_send=False, should_update_state=False)


def decide_journal_update(
    *,
    had_previous_journal_state: bool,
    current_max_journal_id: int,
    previous_last_journal_id: int,
    has_new_journal_descriptions: bool,
    was_issue_previously_notified: bool,
    can_notify_issue_updated: bool,
) -> JournalDecision:
    """Return decision for journal update notification and state tracking."""
    if current_max_journal_id <= previous_last_journal_id:
        return JournalDecision(should_send_update=False, should_update_last_seen=False)

    if not had_previous_journal_state:
        return JournalDecision(should_send_update=False, should_update_last_seen=True)

    should_send = (
        has_new_journal_descriptions and was_issue_previously_notified and can_notify_issue_updated
    )
    return JournalDecision(should_send_update=should_send, should_update_last_seen=True)
