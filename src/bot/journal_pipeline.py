"""Фазы A/B: глобальный поллинг задач и выборка журналов по курсору."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.async_utils import run_in_thread
from database.journal_cursor_repo import get_last_journal_id, upsert_last_journal_id
from database.models import BotUser, CycleSettings
from database.watcher_cache_repo import replace_watchers_for_issue

logger = logging.getLogger("redmine_bot")
_CONTRACT_LOGGED_ISSUES: set[int] = set()
_CONTRACT_TICK_NO = 0


def _redmine_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _cycle_str(session: AsyncSession, key: str, default: str = "") -> str:
    row = await session.scalar(select(CycleSettings.value).where(CycleSettings.key == key))
    return str(row).strip() if row is not None else default


async def _set_cycle_str(session: AsyncSession, key: str, value: str) -> None:
    await session.execute(update(CycleSettings).where(CycleSettings.key == key).values(value=value))


def _to_bool(raw: str, default: bool = False) -> bool:
    s = str(raw or "").strip().lower()
    if not s:
        return default
    return s in ("1", "true", "yes", "on")


def _to_int(raw: str, default: int, *, min_value: int = 1, max_value: int = 1000) -> int:
    try:
        value = int(str(raw or "").strip())
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


async def _contract_audit_settings(session: AsyncSession) -> tuple[bool, int]:
    verbose_raw = await _cycle_str(
        session,
        "CONTRACT_AUDIT_VERBOSE",
        os.getenv("CONTRACT_AUDIT_VERBOSE", "0"),
    )
    sample_raw = await _cycle_str(
        session,
        "CONTRACT_AUDIT_SAMPLE_LIMIT",
        os.getenv("CONTRACT_AUDIT_SAMPLE_LIMIT", "10"),
    )
    return _to_bool(verbose_raw, default=False), _to_int(sample_raw, default=10)


def _parse_watermark(raw: str) -> datetime:
    s = (raw or "").strip()
    if not s:
        return datetime(1970, 1, 1, tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return datetime(1970, 1, 1, tzinfo=UTC)


def _max_updated_on(issues: list[Any]) -> datetime | None:
    best: datetime | None = None
    for iss in issues:
        uo = getattr(iss, "updated_on", None)
        if uo is None:
            continue
        try:
            dt = uo
            if isinstance(dt, str):
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            if getattr(dt, "tzinfo", None) is None:
                dt = dt.replace(tzinfo=UTC)
            dt = dt.astimezone(UTC)
            if best is None or dt > best:
                best = dt
        except (TypeError, ValueError, AttributeError):
            continue
    return best


def _issue_contract_problems(issue: Any) -> list[str]:
    issues: list[str] = []
    if getattr(issue, "id", None) in (None, ""):
        issues.append("missing issue.id")
    if not str(getattr(issue, "subject", "") or "").strip():
        issues.append("missing issue.subject")
    for required in ("project", "status", "priority"):
        node = getattr(issue, required, None)
        if node is None or not str(getattr(node, "name", "") or "").strip():
            issues.append(f"missing {required}.name")
    for optional in ("fixed_version", "assigned_to"):
        node = getattr(issue, optional, None)
        if node is None or not str(getattr(node, "name", "") or "").strip():
            issues.append(f"optional-empty {optional}.name")
    if getattr(issue, "updated_on", None) is None:
        issues.append("missing updated_on")
    return issues


async def phase_a_candidates(
    redmine: Any,
    session: AsyncSession,
    *,
    bot_user_redmine_ids: set[int],
    watched_issue_ids: set[int],
    max_issues: int,
    max_pages: int,
) -> tuple[list[Any], datetime | None]:
    """
    Один проход по Redmine ``updated_on >= LAST_ISSUES_POLL_AT`` без assigned_to/status_id.

    Возвращает (кандидаты в scope, max_updated_on по **всем** строкам ответа для водяного знака).
    """
    global _CONTRACT_TICK_NO
    _CONTRACT_TICK_NO += 1
    first_tick = _CONTRACT_TICK_NO == 1
    contract_verbose, sample_limit = await _contract_audit_settings(session)

    wm_raw = await _cycle_str(session, "LAST_ISSUES_POLL_AT", "")
    wm = _parse_watermark(wm_raw)
    ts = _redmine_ts(wm)

    collected: list[Any] = []
    max_on: datetime | None = None
    offset = 0
    for _page in range(max(1, max_pages)):
        params: dict[str, Any] = {
            "updated_on": f">={ts}",
            "sort": "updated_on:asc",
            "limit": max_issues,
            "offset": offset,
        }
        try:
            batch = await run_in_thread(lambda p=params: list(redmine.issue.filter(**p)))
        except Exception as e:
            logger.error("journal_phase_a_redmine_failed: %s", e, exc_info=True)
            break
        if not batch:
            break
        mo = _max_updated_on(batch)
        if mo is not None and (max_on is None or mo > max_on):
            max_on = mo
        collected.extend(batch)
        if len(batch) < max_issues:
            break
        offset += len(batch)

    in_scope: list[Any] = []
    optional_count = 0
    required_count = 0
    optional_sample_issue_ids: list[int] = []
    required_sample_issue_ids: list[int] = []
    for iss in collected:
        iid = int(getattr(iss, "id", 0) or 0)
        if iid and iid not in _CONTRACT_LOGGED_ISSUES:
            problems = _issue_contract_problems(iss)
            if problems:
                required_problems = [p for p in problems if not p.startswith("optional-empty ")]
                optional_problems = [p for p in problems if p.startswith("optional-empty ")]
                if contract_verbose:
                    logger.info(
                        "journal_contract_check issue_id=%s: %s",
                        iid,
                        ", ".join(problems),
                    )
                else:
                    if required_problems:
                        required_count += 1
                        if len(required_sample_issue_ids) < sample_limit:
                            required_sample_issue_ids.append(iid)
                        logger.warning(
                            "journal_contract_check_required issue_id=%s: %s",
                            iid,
                            ", ".join(required_problems),
                        )
                    if optional_problems:
                        optional_count += 1
                        if len(optional_sample_issue_ids) < sample_limit:
                            optional_sample_issue_ids.append(iid)
                        # В штатном режиме optional-empty уходит в debug после первого тика.
                        if not first_tick:
                            logger.debug(
                                "journal_contract_check_optional issue_id=%s: %s",
                                iid,
                                ", ".join(optional_problems),
                            )
            _CONTRACT_LOGGED_ISSUES.add(iid)
        try:
            aid = getattr(getattr(iss, "assigned_to", None), "id", None)
        except Exception:
            aid = None
        if aid is not None and int(aid) in bot_user_redmine_ids:
            in_scope.append(iss)
            continue
        if iid and iid in watched_issue_ids:
            in_scope.append(iss)
    if not contract_verbose and (required_count or optional_count):
        logger.info(
            "journal_contract_check_summary tick=%s optional_issues=%s required_issues=%s optional_samples=%s required_samples=%s sample_limit=%s",
            _CONTRACT_TICK_NO,
            optional_count,
            required_count,
            optional_sample_issue_ids,
            required_sample_issue_ids,
            sample_limit,
        )
    return in_scope, max_on


async def persist_watermark(session: AsyncSession, max_on: datetime | None) -> None:
    if max_on is None:
        return
    await _set_cycle_str(session, "LAST_ISSUES_POLL_AT", max_on.astimezone(UTC).isoformat())


async def load_bot_user_redmine_ids(session: AsyncSession) -> set[int]:
    r = await session.execute(select(BotUser.redmine_id))
    return {int(x[0]) for x in r.all()}


async def reload_issue_with_journals(redmine: Any, issue_id: int) -> Any:
    return await run_in_thread(
        lambda: redmine.issue.get(issue_id, include=["journals", "watchers"])
    )


async def sync_watcher_cache_for_issue(
    session: AsyncSession,
    issue: Any,
    *,
    redmine_id_to_bot_id: dict[int, int],
) -> None:
    ids: list[int] = []
    try:
        for w in getattr(issue, "watchers", None) or []:
            rid = getattr(w, "id", None)
            if rid is None:
                continue
            bid = redmine_id_to_bot_id.get(int(rid))
            if bid is not None:
                ids.append(bid)
    except Exception:
        pass
    await replace_watchers_for_issue(session, int(issue.id), ids)


async def iter_new_journals_for_issue(
    session: AsyncSession,
    issue: Any,
) -> list[Any]:
    last = await get_last_journal_id(session, int(issue.id))
    try:
        all_j = sorted(list(issue.journals or []), key=lambda j: int(j.id))
    except Exception:
        return []
    return [j for j in all_j if int(j.id) > int(last)]


def aggregate_journals_first_old_last_new(journals: list[Any]) -> Any | None:
    """
    Склеивает несколько journal одной задачи в одно обновление:
    first old -> last new для целевых полей.
    """
    if not journals:
        return None
    ordered = sorted(journals, key=lambda j: int(getattr(j, "id", 0) or 0))
    last = ordered[-1]
    target_fields = {"status_id", "priority_id", "fixed_version_id", "assigned_to_id"}

    merged: dict[str, dict[str, Any]] = {}
    for journal in ordered:
        for detail in list(getattr(journal, "details", None) or []):
            if not isinstance(detail, dict):
                continue
            prop = str(detail.get("name") or detail.get("property") or "").strip()
            if prop not in target_fields:
                continue
            old_val = detail.get("old_value")
            new_val = detail.get("new_value")
            if prop not in merged:
                merged[prop] = {"name": prop, "old_value": old_val, "new_value": new_val}
            else:
                merged[prop]["new_value"] = new_val
    details = [merged[k] for k in sorted(merged.keys())]
    return SimpleNamespace(
        id=int(getattr(last, "id", 0) or 0),
        details=details,
        notes="",
        user=getattr(last, "user", None),
    )


async def advance_cursor_after_journal(
    session: AsyncSession,
    issue_id: int,
    journal_id: int,
) -> None:
    await upsert_last_journal_id(session, issue_id, journal_id)
