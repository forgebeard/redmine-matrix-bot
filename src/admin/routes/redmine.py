"""Redmine user lookup routes: /redmine/users/search, /redmine/users/lookup."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from html import escape as html_escape
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_session

router = APIRouter(tags=["redmine"])


# ── Circuit breaker (перенесён из main.py) ───────────────────────────────────

class _RedmineSearchBreaker:
    """In-memory circuit breaker для поиска пользователей Redmine."""

    def __init__(self):
        self.failures = 0
        self.cooldown_until_ts = 0.0

    def blocked(self) -> bool:
        return datetime.now().timestamp() < self.cooldown_until_ts

    def on_success(self) -> None:
        self.failures = 0
        self.cooldown_until_ts = 0.0

    def on_failure(self) -> None:
        self.failures += 1
        if self.failures >= 5:
            self.cooldown_until_ts = datetime.now().timestamp() + 60


_redmine_search_breaker = _RedmineSearchBreaker()


def _admin() -> object:
    """Late import to avoid circular dependency with main.py."""
    import admin.main as _m
    return _m


def _fetch_redmine_user_by_id(redmine_user_id: int, redmine_url: str, redmine_key: str) -> tuple[dict | None, str | None]:
    """GET /users/:id.json → (user dict, None) или (None, error_code)."""
    if not redmine_url or not redmine_key:
        return None, "not_configured"
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    url = f"{redmine_url.rstrip('/')}/users/{redmine_user_id}.json"
    req = Request(url, headers={"X-Redmine-API-Key": redmine_key})
    try:
        with urlopen(req, timeout=5.0) as r:
            payload = json.loads(r.read().decode("utf-8", errors="replace"))
        u = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(u, dict):
            return None, "bad_response"
        return u, None
    except HTTPError as e:
        if e.code == 404:
            return None, "not_found"
        return None, f"http_{e.code}"
    except URLError:
        return None, "timeout"
    except Exception:
        return None, "error"


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/redmine/users/search", response_class=HTMLResponse)
async def redmine_users_search(
    request: Request,
    session: AsyncSession = Depends(get_session),
    q: str = "",
    limit: int = 20,
):
    """Возвращает HTML-параметры <option> для автозаполнения редмине_id."""
    admin = _admin()
    q = (q or "").strip()
    try:
        limit_i = int(limit)
    except ValueError:
        limit_i = 20
    limit_i = max(1, min(limit_i, 50))

    if not q:
        return HTMLResponse("")

    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    if _redmine_search_breaker.blocked():
        logger = admin.logger
        logger.warning("Redmine search blocked due to cooldown")
        return HTMLResponse('<option value="">Поиск временно недоступен (cooldown)</option>')

    redmine_url = await admin._load_secret_plain(session, "REDMINE_URL")
    redmine_key = await admin._load_secret_plain(session, "REDMINE_API_KEY")

    if not redmine_url or not redmine_key:
        return HTMLResponse('<option value="">Redmine не настроен (нет URL/API key)</option>')

    def _do_search() -> tuple[list[dict], str | None]:
        from urllib.error import HTTPError, URLError
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen

        params = urlencode({"name": q, "limit": str(limit_i)})
        url = f"{redmine_url.rstrip('/')}/users.json?{params}"
        req = Request(url, headers={"X-Redmine-API-Key": redmine_key})
        try:
            with urlopen(req, timeout=5.0) as r:
                payload = json.loads(r.read().decode("utf-8", errors="replace"))
            items = payload.get("users") if isinstance(payload, dict) else []
            return (items if isinstance(items, list) else [], None)
        except HTTPError as e:
            return [], f"http_{e.code}"
        except URLError:
            return [], "timeout"
        except Exception:
            return [], "error"

    users_raw, err = await asyncio.to_thread(_do_search)
    if err:
        _redmine_search_breaker.on_failure()
        return HTMLResponse(f'<option value="">Ошибка поиска: {html_escape(err)}</option>')
    _redmine_search_breaker.on_success()
    users = users_raw

    opts: list[str] = []
    for u in users:
        uid = (u or {}).get("id") if isinstance(u, dict) else None
        if uid is None:
            continue
        firstname = (u or {}).get("firstname", "") if isinstance(u, dict) else ""
        lastname = (u or {}).get("lastname", "") if isinstance(u, dict) else ""
        login = (u or {}).get("login", "") if isinstance(u, dict) else ""
        label = " ".join([s for s in (firstname, lastname) if s]).strip()
        if not label:
            label = login or str(uid)
        opts.append(
            f'<option value="{int(uid)}" data-display-name="{html_escape(label)}">{html_escape(label)}'
            f'{(" (" + html_escape(login) + ")") if login else ""}</option>'
        )
    if not opts:
        return HTMLResponse('<option value="">Ничего не найдено</option>')
    return HTMLResponse("".join(opts))


@router.get("/redmine/users/lookup")
async def redmine_user_lookup(request: Request, user_id: int, session: AsyncSession = Depends(get_session)):
    """JSON для формы пользователя: по числовому Redmine user id подставить отображаемое имя."""
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    if user_id < 1:
        return JSONResponse({"ok": False, "error": "invalid_id"}, status_code=400)
    if _redmine_search_breaker.blocked():
        return JSONResponse({"ok": False, "error": "cooldown"}, status_code=503)

    admin = _admin()
    redmine_url = await admin._load_secret_plain(session, "REDMINE_URL")
    redmine_key = await admin._load_secret_plain(session, "REDMINE_API_KEY")

    raw, err = await asyncio.to_thread(_fetch_redmine_user_by_id, user_id, redmine_url, redmine_key)
    if err == "not_configured":
        return JSONResponse({"ok": False, "error": "not_configured"})
    if err == "not_found":
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    if err:
        _redmine_search_breaker.on_failure()
        return JSONResponse({"ok": False, "error": err}, status_code=502)
    _redmine_search_breaker.on_success()

    firstname = str(raw.get("firstname") or "").strip()
    lastname = str(raw.get("lastname") or "").strip()
    login = str(raw.get("login") or "").strip()
    label = " ".join(s for s in (firstname, lastname) if s).strip()
    if not label:
        label = login or str(user_id)
    return JSONResponse(
        {
            "ok": True,
            "redmine_id": user_id,
            "display_name": label,
            "login": login,
        }
    )
