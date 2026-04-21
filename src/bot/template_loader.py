"""Jinja2-шаблоны v2: файлы в git + override в ``notification_templates``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jinja2 import FileSystemLoader, meta
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy.ext.asyncio import AsyncSession

from database.notification_template_repo import get_template_row

logger = logging.getLogger("redmine_bot")

_TEMPLATE_NAMES = frozenset(
    (
        "tpl_new_issue",
        "tpl_task_change",
        "tpl_reminder",
        "tpl_digest",
        "tpl_test_message",
        "tpl_daily_report",
    )
)


def _default_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "templates" / "bot"


def _sandbox(root: Path) -> SandboxedEnvironment:
    return SandboxedEnvironment(
        loader=FileSystemLoader(str(root)),
        autoescape=True,
    )


def read_default_file(name: str, root: Path | None = None) -> str | None:
    if name not in _TEMPLATE_NAMES:
        return None
    path = (root or _default_root()) / f"{name}.html.j2"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


async def render_named_template(
    session: AsyncSession,
    name: str,
    context: dict[str, Any],
    *,
    root: Path | None = None,
) -> tuple[str, str | None]:
    """
    Рендер HTML и опционально plain: override из БД (``body_html`` / ``body_plain``)
    или файл ``templates/bot/{name}.html.j2``.

    Возвращает ``(html, plain_or_none)``. ``None`` — в БД нет непустого ``body_plain``;
    вызывающий подставляет свой fallback для Matrix ``body``.
    """
    if name not in _TEMPLATE_NAMES:
        raise ValueError(f"unknown template name: {name}")
    r = root or _default_root()
    env = _sandbox(r)
    row = await get_template_row(session, name)
    src = (row.body_html or "").strip() if row is not None else ""
    plain_src = (row.body_plain or "").strip() if row is not None else ""
    if src:
        tpl = env.from_string(src)
    else:
        try:
            tpl = env.get_template(f"{name}.html.j2")
        except Exception as e:
            logger.error("template_file_missing name=%s: %s", name, e)
            raise
    html = tpl.render(**context)
    plain_out: str | None = None
    if plain_src:
        tpl_plain = env.from_string(plain_src)
        plain_out = tpl_plain.render(**context)
    return html, plain_out


def sandbox_accepts_context(name: str, context: dict[str, Any], root: Path | None = None) -> bool:
    """Проверка, что в шаблоне нет неизвестных переменных (для предпросмотра в админке)."""
    r = root or _default_root()
    env = _sandbox(r)
    src = read_default_file(name, r) or ""
    ast = env.parse(src)
    undeclared = meta.find_undeclared_variables(ast)
    return not (set(undeclared) - set(context.keys()))
