"""Единый Jinja-контекст для issue-шаблонов (tpl_new_issue, tpl_task_change, tpl_reminder).

Digest (`tpl_digest`) — отдельная модель: только ``{"items": [...]}``; не использовать
``build_issue_context`` на корне контекста (см. ``digest_service``).
"""

from __future__ import annotations

from typing import Any

from bot.logic import get_version_name
from bot.sender import REDMINE_URL


def _status_display(issue: Any, catalogs: Any | None) -> str:
    raw = str(getattr(getattr(issue, "status", None), "name", "") or "")
    if catalogs is None:
        return raw
    try:
        sid = getattr(getattr(issue, "status", None), "id", None)
        if sid is not None:
            return str(catalogs.status_name(int(sid), default=raw))
    except Exception:
        pass
    return raw


def _priority_display(issue: Any, catalogs: Any | None) -> str:
    raw = str(getattr(getattr(issue, "priority", None), "name", "") or "")
    if catalogs is None:
        return raw
    try:
        pid = getattr(getattr(issue, "priority", None), "id", None)
        if pid is not None:
            return str(catalogs.priority_name(int(pid), default=raw))
    except Exception:
        pass
    return raw


def _version_display(issue: Any) -> str:
    v = get_version_name(issue)
    return str(v).strip() if v else ""


def build_issue_context(
    issue: Any,
    catalogs: Any | None,
    **extra: Any,
) -> dict[str, Any]:
    """Полный набор полей для issue-шаблонов; ``**extra`` переопределяет ключи (event_type, title, …)."""
    try:
        iid = int(getattr(issue, "id", 0) or 0)
    except Exception:
        iid = 0
    base_url = (REDMINE_URL or "").rstrip("/")
    issue_url = f"{base_url}/issues/{iid}" if base_url and iid else (f"/issues/{iid}" if iid else "")
    ctx: dict[str, Any] = {
        "issue_id": iid,
        "issue_url": issue_url,
        "subject": str(getattr(issue, "subject", "") or ""),
        "status": _status_display(issue, catalogs),
        "priority": _priority_display(issue, catalogs),
        "version": _version_display(issue),
        "emoji": "",
        "title": "",
        "event_type": "",
        "extra_text": "",
        "reminder_text": "",
    }
    for k, v in extra.items():
        ctx[str(k)] = v
    return ctx


def preview_issue_context_demo(**overrides: Any) -> dict[str, Any]:
    """Демо-контекст с теми же ключами, что ``build_issue_context`` — для админ-предпросмотра.

    # Sync keys with build_issue_context (issue branch).
    """
    class _FakeIssue:
        id = 101
        subject = "Пример темы"
        status = type("S", (), {"id": 1, "name": "В работе"})()
        priority = type("P", (), {"id": 2, "name": "Нормальный"})()
        fixed_version = type("V", (), {"name": "РЕД ОС 8"})()

    class _FakeCats:
        def status_name(self, rid: int, default: str = "?") -> str:
            return {1: "В работе"}.get(rid, default)

        def priority_name(self, rid: int, default: str = "?") -> str:
            return {2: "Нормальный"}.get(rid, default)

    ctx = build_issue_context(_FakeIssue(), _FakeCats())
    ctx.update(
        {
            "emoji": "📝",
            "title": "Предпросмотр",
            "event_type": "comment",
            "extra_text": "Тестовое описание журнала",
            "reminder_text": "Нет активности 4 ч",
        }
    )
    ctx.update(overrides)
    return ctx
