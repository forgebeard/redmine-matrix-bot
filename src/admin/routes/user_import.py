"""User import from Redmine: scan + bulk-create endpoints."""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BotUser
from database.session import get_session
from user_matcher import scan_redmine_group

logger = logging.getLogger("redmine_admin")

router = APIRouter(tags=["user-import"])

# Rate limit: 1 сканирование раз в 2 минуты
_last_scan_time: float = 0
SCAN_COOLDOWN = 120


def _admin() -> object:
    import admin.main as _m

    return _m


@router.get("/api/users/scan-redmine/check")
async def scan_check(request: Request, session: AsyncSession = Depends(get_session)):
    """Проверяет заполнены ли credentials для сканирования."""
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    redmine_url = await admin._load_secret_plain(session, "REDMINE_URL")
    redmine_key = await admin._load_secret_plain(session, "REDMINE_API_KEY")
    matrix_hs = await admin._load_secret_plain(session, "MATRIX_HOMESERVER")
    matrix_token = await admin._load_secret_plain(session, "MATRIX_ACCESS_TOKEN")

    ready = bool(redmine_url and redmine_key and matrix_hs and matrix_token)
    return JSONResponse({"ready": ready})


@router.post("/api/users/scan-redmine")
async def scan_redmine(
    request: Request,
    target_url: Annotated[str, Form()],
    session: AsyncSession = Depends(get_session),
):
    """Сканирует группу Redmine и сопоставляет сотрудников с Matrix.

    Возвращает JSON со списком Match.
    """
    global _last_scan_time

    admin = _admin()
    admin._verify_csrf_json(request)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    # Rate limit
    now = time.monotonic()
    if now - _last_scan_time < SCAN_COOLDOWN:
        remaining = int(SCAN_COOLDOWN - (now - _last_scan_time))
        return JSONResponse(
            {"error": f"Подождите {remaining}с перед следующим сканированием"},
            status_code=429,
        )

    # Загружаем credentials
    redmine_url = await admin._load_secret_plain(session, "REDMINE_URL")
    redmine_key = await admin._load_secret_plain(session, "REDMINE_API_KEY")
    matrix_hs = await admin._load_secret_plain(session, "MATRIX_HOMESERVER")
    matrix_token = await admin._load_secret_plain(session, "MATRIX_ACCESS_TOKEN")

    if not all([redmine_url, redmine_key, matrix_hs, matrix_token]):
        return JSONResponse(
            {
                "error": "Заполните Параметры сервиса (Redmine URL, API-ключ, Matrix homeserver, токен)"
            },
            status_code=400,
        )

    # Получаем existing redmine_ids
    result = await session.execute(select(BotUser.redmine_id))
    existing_ids = set(result.scalars().all())

    # Сканируем
    try:
        matches = await scan_redmine_group(
            target_url=target_url,
            redmine_url=redmine_url,
            redmine_api_key=redmine_key,
            matrix_homeserver=matrix_hs,
            matrix_access_token=matrix_token,
            existing_redmine_ids=existing_ids,
        )
    except Exception as e:
        logger.error("Scan redmine failed: %s", e, exc_info=True)
        return JSONResponse({"error": f"Ошибка сканирования: {e}"}, status_code=500)

    _last_scan_time = time.monotonic()

    return JSONResponse(
        {
            "matches": [
                {
                    "redmine_name": m.redmine_name,
                    "redmine_id": m.redmine_id,
                    "matrix_localpart": m.matrix_localpart,
                    "matrix_display_name": m.matrix_display_name,
                    "status": m.status,
                }
                for m in matches
            ],
            "total": len(matches),
            "found": sum(1 for m in matches if m.is_found),
            "existing": sum(1 for m in matches if m.is_existing),
            "not_found": sum(1 for m in matches if m.status == "not_found"),
        }
    )


@router.post("/api/users/bulk-create")
async def bulk_create_users(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Массовое создание пользователей из результатов сканирования.

    Принимает JSON: {"users": [{"redmine_id": 123, "matrix_localpart": "denis_fomichev", ...}, ...]}
    """

    from database.models import BotUser

    admin = _admin()
    admin._verify_csrf_json(request)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    body = await request.json()
    users_data = body.get("users", [])
    if not users_data:
        return JSONResponse({"error": "Нет пользователей для создания"}, status_code=400)

    # Получаем timezone из настроек
    tz_name = await admin._load_secret_plain(session, "__service_timezone")
    default_tz = tz_name or "Europe/Moscow"

    created = []
    skipped = []
    errors = []

    for u in users_data:
        rid = u.get("redmine_id")
        localpart = u.get("matrix_localpart", "")
        display_name = u.get("redmine_name", "")

        if not rid:
            errors.append({"redmine_id": rid, "error": "Нет redmine_id"})
            continue

        # Проверяем дубликат
        existing = await session.execute(select(BotUser.id).where(BotUser.redmine_id == rid))
        if existing.scalar_one_or_none():
            skipped.append({"redmine_id": rid, "reason": "уже существует"})
            continue

        # Формируем room_id из localpart
        # Загружаем MATRIX_USER_ID чтобы узнать домен
        bot_mxid = await admin._load_secret_plain(session, "MATRIX_USER_ID")
        domain = bot_mxid.split(":", 1)[1] if bot_mxid and ":" in bot_mxid else ""
        room_id = f"@{localpart}:{domain}" if domain else f"@{localpart}"

        try:
            row = BotUser(
                redmine_id=rid,
                display_name=display_name.strip() or None,
                room=room_id,
                notify=["all"],
                timezone=default_tz,
                work_hours=None,
                work_days=None,
                dnd=False,
            )
            session.add(row)
            await session.flush()
            created.append({"redmine_id": rid, "id": row.id})
        except Exception as e:
            errors.append({"redmine_id": rid, "error": str(e)})

    await session.commit()

    return JSONResponse(
        {
            "created": created,
            "skipped": skipped,
            "errors": errors,
            "total_created": len(created),
            "total_skipped": len(skipped),
            "total_errors": len(errors),
        }
    )
