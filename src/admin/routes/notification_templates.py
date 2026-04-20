"""API шаблонов уведомлений v2 (Jinja2 + таблица ``notification_templates``)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy.ext.asyncio import AsyncSession

from admin.template_blocks import (
    BLOCK_EDITOR_TEMPLATES,
    BlockConfig,
    compile_blocks_to_jinja,
    default_block_configs_as_dicts,
    jinja_to_blocks,
    registry_json_objects,
)
from bot.template_context import preview_issue_context_demo
from bot.template_loader import read_default_file
from database.notification_template_repo import (
    NOTIFICATION_TEMPLATE_LABELS,
    TEMPLATE_NAMES,
    clear_override,
    get_template_row,
    list_all_templates,
    upsert_template_body,
)
from database.session import get_session

router = APIRouter(tags=["notification-templates"])


def _admin() -> object:
    import admin.main as _m

    return _m


def _mock_context_for_preview(name: str) -> dict[str, Any]:
    # Sync keys with bot.template_context.preview_issue_context_demo / build_issue_context.
    # tpl_digest / tpl_dry_run — отдельные контракты, не issue-«полный контекст».
    if name == "tpl_digest":
        return {
            "items": [
                {"issue_id": 1, "subject": "Задача A", "events": ["comment"]},
                {"issue_id": 2, "subject": "Задача B", "events": ["status_change"]},
            ]
        }
    if name == "tpl_dry_run":
        return {
            "issue_id": 101,
            "issue_url": "https://redmine.example/issues/101",
            "subject": "Пример темы",
        }
    if name == "tpl_reminder":
        return preview_issue_context_demo(emoji="⏰", title="Напоминание")
    return preview_issue_context_demo()


async def _effective_body_html(session: AsyncSession, name: str) -> str:
    row = await get_template_row(session, name)
    if row and (row.body_html or "").strip():
        return (row.body_html or "").strip()
    return (read_default_file(name) or "").strip()


def _parse_block_configs(raw: Any) -> list[BlockConfig]:
    if not isinstance(raw, list):
        raise ValueError("blocks must be a list")
    out: list[BlockConfig] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each block must be an object")
        bid = str(item.get("block_id", "")).strip()
        if not bid:
            raise ValueError("block_id required")
        settings = item.get("settings")
        if settings is not None and not isinstance(settings, dict):
            raise ValueError("settings must be an object")
        out.append(
            BlockConfig(
                block_id=bid,
                enabled=bool(item.get("enabled", True)),
                order=int(item.get("order", 0)),
                settings={str(k): str(v) for k, v in (settings or {}).items()},
            )
        )
    return out


@router.post("/api/bot/notification-templates/compile-blocks", response_class=JSONResponse)
async def notification_templates_compile_blocks(request: Request):
    admin = _admin()
    admin._verify_csrf_json(request)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Некорректный JSON") from exc

    template_name = str(data.get("template_name") or "tpl_new_issue").strip()
    if template_name not in BLOCK_EDITOR_TEMPLATES:
        raise HTTPException(400, "Шаблон не поддерживает редактор блоков")

    try:
        blocks = _parse_block_configs(data.get("blocks"))
        jinja_str = compile_blocks_to_jinja(blocks)
    except (ValueError, TypeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    ctx = _mock_context_for_preview(template_name)
    env = SandboxedEnvironment(autoescape=True)
    try:
        html_preview = env.from_string(jinja_str).render(**ctx)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": "Template render failed", "details": str(e)},
            status_code=400,
        )

    return {"ok": True, "jinja": jinja_str, "html_preview": html_preview}


@router.get("/api/bot/notification-templates/block-registry", response_class=JSONResponse)
async def notification_templates_block_registry(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    return {"ok": True, "blocks": registry_json_objects()}


@router.get("/api/bot/notification-templates", response_class=JSONResponse)
async def notification_templates_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")

    rows = {r.name: r for r in await list_all_templates(session)}
    out: list[dict[str, Any]] = []
    for name in TEMPLATE_NAMES:
        row = rows.get(name)
        default_html = read_default_file(name) or ""
        out.append(
            {
                "name": name,
                "display_name": NOTIFICATION_TEMPLATE_LABELS.get(name, name),
                "default_html": default_html,
                "override_html": (row.body_html if row else None),
                "override_plain": (row.body_plain if row else None),
                "updated_at": (row.updated_at.isoformat() if row and row.updated_at else None),
            }
        )
    return {"ok": True, "templates": out}


@router.put("/api/bot/notification-templates/{name}", response_class=JSONResponse)
async def notification_templates_put(
    request: Request,
    name: str,
    body_html: Annotated[str, Form()] = "",
    body_plain: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    if name not in TEMPLATE_NAMES:
        raise HTTPException(400, "Неизвестное имя шаблона")

    ctx = _mock_context_for_preview(name)
    src = (body_html or "").strip()
    if src:
        env = SandboxedEnvironment(autoescape=True)
        try:
            env.from_string(src).render(**ctx)
        except Exception as e:
            raise HTTPException(400, f"Ошибка шаблона: {e}") from e

    await upsert_template_body(
        session,
        name=name,
        body_html=body_html.strip() or None,
        body_plain=(body_plain.strip() or None) if body_plain else None,
        updated_by=getattr(user, "login", None) or getattr(user, "email", None),
    )
    await session.commit()
    return {"ok": True}


@router.post("/api/bot/notification-templates/{name}/reset", response_class=JSONResponse)
async def notification_templates_reset(
    request: Request,
    name: str,
    csrf_token: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf(request, csrf_token)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    if name not in TEMPLATE_NAMES:
        raise HTTPException(400, "Неизвестное имя шаблона")
    await clear_override(session, name)
    await session.commit()
    return {"ok": True}


@router.get("/api/bot/notification-templates/{name}/decompose", response_class=JSONResponse)
async def notification_templates_decompose(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_session),
):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    if name not in TEMPLATE_NAMES:
        raise HTTPException(400, "Неизвестное имя шаблона")
    if name not in BLOCK_EDITOR_TEMPLATES:
        raise HTTPException(400, "Редактор блоков недоступен для этого шаблона")

    body_html = await _effective_body_html(session, name)
    blocks = jinja_to_blocks(body_html, name)
    return {
        "ok": True,
        "blocks": [asdict(b) for b in blocks] if blocks else None,
        "is_custom_jinja": blocks is None,
        "body_html": body_html,
        "default_blocks": default_block_configs_as_dicts(name),
    }


@router.post("/api/bot/notification-templates/{name}/decompose-body", response_class=JSONResponse)
async def notification_templates_decompose_body(
    request: Request,
    name: str,
):
    admin = _admin()
    admin._verify_csrf_json(request)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    if name not in TEMPLATE_NAMES:
        raise HTTPException(400, "Неизвестное имя шаблона")
    if name not in BLOCK_EDITOR_TEMPLATES:
        raise HTTPException(400, "Редактор блоков недоступен для этого шаблона")
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Некорректный JSON") from exc
    body_html = str(data.get("body_html", ""))
    blocks = jinja_to_blocks(body_html, name)
    return {
        "ok": True,
        "blocks": [asdict(b) for b in blocks] if blocks else None,
        "is_custom_jinja": blocks is None,
        "default_blocks": default_block_configs_as_dicts(name),
    }


@router.post("/api/bot/notification-templates/preview", response_class=JSONResponse)
async def notification_templates_preview(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    admin = _admin()
    admin._verify_csrf_json(request)
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "role", "") != "admin":
        raise HTTPException(403, "Только admin")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Некорректный JSON") from exc
    name = str(payload.get("name") or "").strip()
    if name not in TEMPLATE_NAMES:
        raise HTTPException(400, "Неизвестное имя шаблона")
    html_src = str(payload.get("body_html") or "").strip()
    ctx = payload.get("context")
    if not isinstance(ctx, dict):
        ctx = _mock_context_for_preview(name)
    env = SandboxedEnvironment(autoescape=True)
    try:
        if html_src:
            html = env.from_string(html_src).render(**ctx)
        else:
            default = read_default_file(name) or ""
            html = env.from_string(default).render(**ctx)
    except Exception as e:
        raise HTTPException(400, f"Ошибка рендера: {e}") from e
    return {"ok": True, "html": html}
