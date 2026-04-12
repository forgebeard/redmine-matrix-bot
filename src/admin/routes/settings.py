"""Settings routes: /onboarding, /settings/db-config."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AppSecret
from database.session import get_session
from security import decrypt_secret, encrypt_secret, load_master_key

logger = logging.getLogger("redmine_bot")
router = APIRouter(tags=["settings"])

_ENV_FILE_PATH = Path("/app/.env")

_SECRET_NAMES = [
    "REDMINE_URL",
    "REDMINE_API_KEY",
    "MATRIX_HOMESERVER",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_USER_ID",
]

# Поля которые НЕ нужно маскировать (URL'ы и MXID)
_UNMASKED_SECRETS = {"REDMINE_URL", "MATRIX_HOMESERVER", "MATRIX_USER_ID"}


def _check_redmine_access(url: str, api_key: str) -> tuple[bool, str]:
    base = (url or "").strip().rstrip("/")
    key = (api_key or "").strip()
    logger.info("Redmine check: URL=%s, KeyLen=%d", base, len(key))
    
    if not base or not key:
        return False, "Redmine: укажите URL и API-ключ."
    
    # Проверка на нелатинские символы ДО запроса
    try:
        key.encode("ascii")
    except UnicodeEncodeError:
        logger.error("Redmine key contains non-ASCII chars: %s", repr(key))
        return False, f"Redmine: API-ключ содержит недопустимые символы (нужен только английский)."

    target_url = f"{base}/users/current.json"
    
    try:
        with httpx.Client(timeout=6.0) as client:
            resp = client.get(
                target_url,
                headers={"X-Redmine-API-Key": key, "Accept": "application/json"},
            )
            logger.info("Redmine response status: %d", resp.status_code)
            
            if resp.status_code != 200:
                return False, f"Redmine: HTTP {resp.status_code}."
            
            data = resp.json()
            user = data.get("user") if isinstance(data, dict) else {}
            login = str((user or {}).get("login") or "").strip()
            suffix = f" (user: {login})" if login else ""
            return True, f"Redmine: подключение успешно{suffix}."
    except httpx.ConnectError as e:
        logger.error("Redmine ConnectError: %s", e)
        return False, f"Redmine: нет ответа (URL/сеть)."
    except Exception as e:
        logger.error("Redmine UNEXPECTED ERROR: %s", e, exc_info=True)
        return False, f"Redmine: ошибка проверки ({type(e).__name__}: {e})."


def _check_matrix_access(homeserver: str, user_id: str, token: str) -> tuple[bool, str]:
    hs = (homeserver or "").strip().rstrip("/")
    mxid = (user_id or "").strip()
    access_token = (token or "").strip()
    logger.info("Matrix check: HS=%s, UID=%s, TokLen=%d", hs, mxid, len(access_token))
    
    if not hs or not mxid or not access_token:
        return False, "Matrix: укажите homeserver, user id и token."

    # Проверка на нелатинские символы ДО запроса
    try:
        access_token.encode("ascii")
    except UnicodeEncodeError:
        logger.error("Matrix token contains non-ASCII chars: %s", repr(access_token))
        return False, f"Matrix: Токен содержит недопустимые символы (нужен только английский)."
    
    versions_url = f"{hs}/_matrix/client/versions"
    
    try:
        with httpx.Client(timeout=6.0) as client:
            # 1. Проверка доступности сервера
            resp = client.get(versions_url)
            logger.info("Matrix versions status: %d", resp.status_code)
            if resp.status_code != 200:
                return False, f"Matrix: HTTP {resp.status_code}."
            
            # 2. Проверка токена
            whoami_url = f"{hs}/_matrix/client/v3/account/whoami"
            who_resp = client.get(
                whoami_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            logger.info("Matrix whoami status: %d", who_resp.status_code)
            
            if who_resp.status_code != 200:
                return False, f"Matrix: токен недействителен (HTTP {who_resp.status_code})."
            
            data = who_resp.json()
            got_user = data.get("user_id", "")
            if got_user and got_user != mxid:
                return True, f"Matrix: подключение успешно, но token принадлежит {got_user}."
            return True, "Matrix: подключение успешно."
    except httpx.ConnectError as e:
        logger.error("Matrix ConnectError: %s", e)
        return False, f"Matrix: нет ответа (URL/сеть)."
    except Exception as e:
        logger.error("Matrix UNEXPECTED ERROR: %s", e, exc_info=True)
        return False, f"Matrix: ошибка проверки ({type(e).__name__}: {e})."


def _mask_secret_value(name: str, value: str) -> str:
    """Маскирует секрет. URL'ы и MXID не маскируются."""
    if name in _UNMASKED_SECRETS:
        return value
    if not value or len(value) <= 8:
        return "••••••••"
    return value[:4] + "•" * (len(value) - 8) + value[-4:]


def _load_db_config_from_env() -> dict[str, str]:
    """Читает DB credentials из .env файла."""
    if not _ENV_FILE_PATH.exists():
        return {
            "postgres_user": "bot",
            "postgres_db": "via",
            "postgres_password": "",
            "app_master_key": "",
        }

    config = {}
    for line in _ENV_FILE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()

    return {
        "postgres_user": config.get("POSTGRES_USER", "bot"),
        "postgres_db": config.get("POSTGRES_DB", "via"),
        "postgres_password": config.get("POSTGRES_PASSWORD", ""),
        "app_master_key": config.get("APP_MASTER_KEY", ""),
    }


def _update_env_file(updates: dict[str, str]) -> None:
    """Обновляет переменные в .env файле, сохраняя остальные."""
    if not _ENV_FILE_PATH.exists():
        raise RuntimeError(".env file not found")

    lines = _ENV_FILE_PATH.read_text(encoding="utf-8").splitlines()
    new_lines = []
    updated_keys = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    _ENV_FILE_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _admin() -> object:
    """Late import to avoid circular dependency with main.py."""
    import admin.main as _m

    return _m


@router.get("/settings/db-config", response_class=JSONResponse)
async def get_db_config(request: Request, session: AsyncSession = Depends(get_session)):
    """Возвращает текущие DB credentials из .env (только для admin)."""
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    config = _load_db_config_from_env()
    return {
        "ok": True,
        "postgres_user": config["postgres_user"],
        "postgres_db": config["postgres_db"],
        "postgres_password": config["postgres_password"],
        "app_master_key": config["app_master_key"],
    }


@router.post("/settings/db-config/regenerate", response_class=JSONResponse)
async def regenerate_db_config(
    request: Request,
    regenerate_password: Annotated[str, Form()] = "1",
    regenerate_key: Annotated[str, Form()] = "1",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    """Генерирует новые credentials и обновляет .env."""
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    admin._verify_csrf(request, csrf_token)

    new_password = secrets.token_urlsafe(24) if regenerate_password == "1" else None
    new_master_key = secrets.token_hex(32) if regenerate_key == "1" else None

    updates = {}
    if new_password:
        updates["POSTGRES_PASSWORD"] = new_password
    if new_master_key:
        updates["APP_MASTER_KEY"] = new_master_key

    if updates:
        _update_env_file(updates)

    if new_master_key:
        key = new_master_key.encode("utf-8")
        rows = await session.execute(select(AppSecret))
        for row in rows.scalars().all():
            try:
                old_val = decrypt_secret(row.ciphertext, row.nonce, load_master_key())
                enc = encrypt_secret(old_val, key)
                row.ciphertext = enc.ciphertext
                row.nonce = enc.nonce
                row.key_version = enc.key_version
            except Exception:
                pass
        await session.commit()

    if new_password:
        from sqlalchemy import text

        try:
            cfg = _load_db_config_from_env()
            sync_url = os.environ.get("DATABASE_URL", "").replace(
                cfg["postgres_password"], new_password
            )
            if sync_url:
                engine_url = admin.sync_database_url_for_alembic(sync_url)
                from sqlalchemy import create_engine

                eng = create_engine(engine_url)
                with eng.connect() as c:
                    c.execute(
                        text(f"ALTER USER {cfg['postgres_user']} WITH PASSWORD '{new_password}'")
                    )
                    c.commit()
                eng.dispose()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    result = {"ok": True}
    if new_password:
        result["postgres_password"] = new_password
    if new_master_key:
        result["app_master_key"] = new_master_key
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Onboarding / Настройки сервиса
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, session: AsyncSession = Depends(get_session)):
    """Страница настроек сервиса (onboarding)."""
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    # Загружаем секреты из БД
    secrets_raw: dict[str, str] = {}
    secrets_masked: dict[str, str] = {}

    rows = await session.execute(select(AppSecret))
    for row in rows.scalars().all():
        try:
            val = decrypt_secret(row.ciphertext, row.nonce, load_master_key())
            secrets_raw[row.name] = val
            secrets_masked[row.name] = _mask_secret_value(row.name, val)
        except Exception:
            secrets_masked[row.name] = "••••••••"

    notify_catalog, versions_catalog = await admin._load_catalogs(session)
    csrf_token, _ = admin._ensure_csrf(request)
    error = request.query_params.get("error", "")
    db_config = _load_db_config_from_env()

    # Таймзоны
    tz_all = admin._standard_timezone_options()
    tz_labels = admin._timezone_labels(tz_all)
    # Текущая таймзона сервиса (из секретов)
    current_tz = secrets_raw.get("SERVICE_TIMEZONE", "") or os.getenv("BOT_TIMEZONE", "Europe/Moscow")

    return admin.templates.TemplateResponse(
        request,
        "panel/onboarding.html",
        {
            "secrets_raw": secrets_raw,
            "secrets_masked": secrets_masked,
            "notify_catalog": notify_catalog,
            "versions_catalog": versions_catalog,
            "csrf_token": csrf_token,
            "error": error,
            "db_config": db_config,
            "timezone_all_options": tz_all,
            "timezone_labels": tz_labels,
            "service_timezone": current_tz,
        },
    )


@router.post("/onboarding/save")
async def onboarding_save(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    """Сохраняет параметры сервиса (секреты) из формы onboarding."""
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    admin._verify_csrf(request, csrf_token)

    form = await request.form()

    for secret_name in _SECRET_NAMES:
        raw = form.get(f"secret_{secret_name}", "")
        if not raw:
            continue
        # Проверяем, не маскированное ли это значение (содержит •)
        if "•" in raw:
            continue
        existing = await session.execute(
            select(AppSecret).where(AppSecret.name == secret_name)
        )
        row = existing.scalar_one_or_none()
        enc = encrypt_secret(raw, load_master_key())
        if row:
            row.ciphertext = enc.ciphertext
            row.nonce = enc.nonce
        else:
            session.add(AppSecret(name=secret_name, ciphertext=enc.ciphertext, nonce=enc.nonce))

    await session.commit()
    return RedirectResponse("/onboarding", status_code=303)


@router.post("/onboarding/check")
async def onboarding_check(
    request: Request,
    secret_REDMINE_URL: Annotated[str, Form()] = "",
    secret_REDMINE_API_KEY: Annotated[str, Form()] = "",
    secret_MATRIX_HOMESERVER: Annotated[str, Form()] = "",
    secret_MATRIX_USER_ID: Annotated[str, Form()] = "",
    secret_MATRIX_ACCESS_TOKEN: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    """Проверяет доступность Redmine и Matrix."""
    logger.info("=== ONBOARDING CHECK STARTED ===")
    
    admin = _admin()
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    admin._verify_csrf(request, csrf_token)

    # 1. Загружаем реальные секреты из БД, чтобы использовать их, если форма прислала маскированные (•••)
    db_secrets: dict[str, str] = {}
    rows = await session.execute(select(AppSecret))
    for row in rows.scalars().all():
        try:
            db_secrets[row.name] = decrypt_secret(row.ciphertext, row.nonce, load_master_key())
        except Exception:
            pass

    def _resolve(secret_name: str, form_value: str) -> str:
        """Если значение маскировано (•), берем из БД. Иначе берем из формы."""
        if "•" in form_value:
            return db_secrets.get(secret_name, form_value)
        return form_value

    # 2. Разрешаем значения
    redmine_url = _resolve("REDMINE_URL", secret_REDMINE_URL)
    redmine_key = _resolve("REDMINE_API_KEY", secret_REDMINE_API_KEY)
    matrix_hs = _resolve("MATRIX_HOMESERVER", secret_MATRIX_HOMESERVER)
    matrix_uid = _resolve("MATRIX_USER_ID", secret_MATRIX_USER_ID)
    matrix_tok = _resolve("MATRIX_ACCESS_TOKEN", secret_MATRIX_ACCESS_TOKEN)

    # 3. Проверяем
    logger.info("Calling _check_redmine_access...")
    redmine_ok, redmine_msg = await asyncio.to_thread(
        _check_redmine_access,
        redmine_url,
        redmine_key,
    )
    logger.info("Redmine result: ok=%s, msg=%s", redmine_ok, redmine_msg)
    
    logger.info("Calling _check_matrix_access...")
    matrix_ok, matrix_msg = await asyncio.to_thread(
        _check_matrix_access,
        matrix_hs,
        matrix_uid,
        matrix_tok,
    )
    logger.info("Matrix result: ok=%s, msg=%s", matrix_ok, matrix_msg)
    
    ok = redmine_ok and matrix_ok
    logger.info("Final check result: ok=%s", ok)
    
    return JSONResponse(
        {
            "ok": ok,
            "checks": [
                {"service": "redmine", "ok": redmine_ok, "message": redmine_msg},
                {"service": "matrix", "ok": matrix_ok, "message": matrix_msg},
            ],
        }
    )
