"""Events routes: /events, /events/export.csv, /audit redirects."""

from __future__ import annotations

import re
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from events_log_display import (
    events_log_to_csv_bytes,
    filter_parsed_lines_by_local_date,
    parse_events_log_for_table,
    parse_ui_date_param,
)
from ui_datetime import bot_display_timezone

router = APIRouter(tags=["events"])


def _admin() -> object:
    """Late import to avoid circular dependency with main.py."""
    import admin.main as _m

    return _m


# ── Helpers ──────────────────────────────────────────────────────────────────


def _events_filter_query_dict(
    date_from: str,
    date_to: str,
    time_at: str,
    page_size: int,
) -> dict[str, str]:
    d: dict[str, str] = {"page_size": str(page_size)}
    if date_from.strip():
        d["date_from"] = date_from.strip()
    if date_to.strip():
        d["date_to"] = date_to.strip()
    if time_at.strip():
        d["time_at"] = time_at.strip()
    return d


def _normalize_time_filter(value) -> str:
    """Приводит value к строке, обрабатывает списки/кортежи."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    raw = str(value).strip()
    if not raw:
        return ""
    if re.fullmatch(r"\d{2}:\d{2}", raw):
        return raw
    return ""


def _load_filtered_event_lines(date_from_s, date_to_s, time_at_s):
    admin = _admin()
    path = admin._admin_events_log_path()
    raw, truncated = admin._read_events_log_scan(
        path, max_bytes=admin._admin_events_log_scan_bytes()
    )
    parsed = parse_events_log_for_table(raw)
    tz = bot_display_timezone()
    df = parse_ui_date_param(date_from_s)
    d_to = parse_ui_date_param(date_to_s)
    if (
        len(parsed) == 1
        and parsed[0].sort_key is None
        and (
            "Файл лога не найден" in (parsed[0].message or "")
            or "Не удалось прочитать" in (parsed[0].message or "")
        )
    ):
        filtered = parsed
    else:
        filtered = filter_parsed_lines_by_local_date(parsed, df, d_to, tz)

    time_filter = _normalize_time_filter(time_at_s)

    if time_filter and filtered:
        tf = str(time_filter)
        filtered = [
            row for row in filtered if str(getattr(row, "time_ui", "") or "").startswith(tf)
        ]
    return filtered, truncated, path


# ── GET /events ──────────────────────────────────────────────────────────────


@router.get("/events", response_class=HTMLResponse)
async def events_page(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    time_at: str = "",
    page: int = 1,
    page_size: int = 50,
):
    safe_date_from = str(date_from) if date_from else ""
    safe_date_to = str(date_to) if date_to else ""
    safe_time_at = str(time_at) if time_at else ""

    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    try:
        page_i = max(1, int(page))
    except (TypeError, ValueError):
        page_i = 1
    try:
        page_size_i = int(page_size)
    except (TypeError, ValueError):
        page_size_i = 50
    page_size_i = min(200, max(5, page_size_i))

    rows, truncated, _log_path = _load_filtered_event_lines(
        safe_date_from, safe_date_to, safe_time_at
    )
    normalized_time = _normalize_time_filter(safe_time_at)
    total = len(rows)
    total_pages = max(1, (total + page_size_i - 1) // page_size_i) if total > 0 else 1
    page_i = max(1, min(page_i, total_pages))
    offset = (page_i - 1) * page_size_i
    page_rows = rows[offset : offset + page_size_i]

    qdict = _events_filter_query_dict(date_from, date_to, normalized_time, page_size_i)
    qs_base = urlencode(qdict)
    events_filter_link_prefix = f"/events?{qs_base}&" if qs_base else "/events?"

    admin = _admin()
    return admin.templates.TemplateResponse(
        request,
        "panel/events.html",
        {
            "rows": page_rows,
            "total": total,
            "page": page_i,
            "page_size": page_size_i,
            "total_pages": total_pages,
            "filter_date_from": date_from,
            "filter_date_to": date_to,
            "filter_time_at": normalized_time,
            "events_filter_link_prefix": events_filter_link_prefix,
            "export_qs": qs_base,
            "events_log_truncated": truncated,
        },
    )


# ── GET /audit (redirect) ───────────────────────────────────────────────────


@router.get("/audit")
async def audit_legacy_redirect(request: Request):
    """Старый URL: журнал перенесён на /events."""
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    q = request.url.query
    loc = f"/events?{q}" if q else "/events"
    return RedirectResponse(loc, status_code=303)


# ── GET /events/export.csv ───────────────────────────────────────────────────


@router.get("/events/export.csv")
async def events_export_csv(
    request: Request,
    date_from: str = "",
    date_to: str = "",
    time_at: str = "",
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    safe_date_from = str(date_from) if date_from else ""
    safe_date_to = str(date_to) if date_to else ""
    safe_time_at = str(time_at) if time_at else ""

    rows, _truncated, _path = _load_filtered_event_lines(safe_date_from, safe_date_to, safe_time_at)
    body = events_log_to_csv_bytes(rows, max_rows=50_000)
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="events_log_{stamp}.csv"'},
    )


# ── GET /audit/export.csv (redirect) ─────────────────────────────────────────


@router.get("/audit/export.csv")
async def audit_export_legacy_redirect(request: Request):
    """Старый URL: выгрузка перенесена на /events/export.csv."""
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    q = request.url.query
    loc = f"/events/export.csv?{q}" if q else "/events/export.csv"
    return RedirectResponse(loc, status_code=303)
