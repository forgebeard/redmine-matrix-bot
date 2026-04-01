"""Redmine: вспомогательные запросы из админки (поиск пользователей)."""

from __future__ import annotations

import asyncio
import json
from html import escape as html_escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from admin.authz import require_admin
from admin.constants import REDMINE_API_KEY, REDMINE_URL
from admin.runtime import logger, redmine_search_breaker
from database.app_secret_values import load_decrypted_secrets, merge_secret
from database.session import get_session

router = APIRouter()


async def _redmine_url_and_key(session: AsyncSession) -> tuple[str, str]:
    db = await load_decrypted_secrets(session, ("REDMINE_URL", "REDMINE_API_KEY"))
    url = merge_secret(db, "REDMINE_URL", REDMINE_URL)
    key = merge_secret(db, "REDMINE_API_KEY", REDMINE_API_KEY)
    return url, key


@router.get("/redmine/users/search", response_class=HTMLResponse)
async def redmine_users_search(
    request: Request,
    session: AsyncSession = Depends(get_session),
    q: str = "",
    limit: int = 20,
):
    """
    Возвращает HTML-параметры <option> для автозаполнения редмине_id.

    Важно: endpoint может быть использован даже без доступной Redmine-конфигурации —
    тогда просто вернёт пустой ответ.
    """
    q = (q or "").strip()
    try:
        limit_i = int(limit)
    except ValueError:
        limit_i = 20
    limit_i = max(1, min(limit_i, 50))

    if not q:
        return HTMLResponse("")

    require_admin(request)
    if redmine_search_breaker.blocked():
        logger.warning("Redmine search blocked due to cooldown")
        return HTMLResponse('<option value="">Поиск временно недоступен (cooldown)</option>')

    redmine_url, redmine_key = await _redmine_url_and_key(session)
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
        redmine_search_breaker.on_failure()
        return HTMLResponse(f'<option value="">Ошибка поиска: {html_escape(err)}</option>')
    redmine_search_breaker.on_success()
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
