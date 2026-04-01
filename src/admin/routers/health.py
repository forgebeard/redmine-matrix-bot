"""Health / readiness / SMTP probe."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import BotAppUser
from database.session import get_session
from mail import check_smtp_health
from ops.docker_control import DockerControlError, get_service_status
from security import SecurityError, load_master_key

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/health/live")
async def health_live():
    return {"status": "live"}


@router.get("/health/ready")
async def health_ready(session: AsyncSession = Depends(get_session)):
    try:
        await session.execute(select(BotAppUser.id).limit(1))
        load_master_key()
        get_service_status()
    except SecurityError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except DockerControlError as e:
        raise HTTPException(status_code=503, detail=f"runtime backend: {e}") from e
    except Exception:
        raise HTTPException(status_code=503, detail="service not ready")
    return {"status": "ready"}


@router.get("/health/smtp")
async def health_smtp():
    health = check_smtp_health()
    code = 200 if health.ok else 503
    return HTMLResponse(
        content=json.dumps(
            {
                "status": "ok" if health.ok else "degraded",
                "detail": health.detail,
                "checked_at": health.checked_at,
            },
            ensure_ascii=False,
        ),
        status_code=code,
        media_type="application/json",
    )
