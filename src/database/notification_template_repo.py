"""Шаблоны уведомлений v2: ``notification_templates``."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import NotificationTemplate


TEMPLATE_NAMES = (
    "tpl_new_issue",
    "tpl_task_change",
    "tpl_reminder",
    "tpl_digest",
    "tpl_dry_run",
)

# Подписи в админке (заголовки карточек, тосты); технический ключ — ``name`` в API.
NOTIFICATION_TEMPLATE_LABELS: dict[str, str] = {
    "tpl_new_issue": "Новая задача",
    "tpl_task_change": "Изменение задачи",
    "tpl_reminder": "Напоминание",
    "tpl_digest": "Дайджест",
    "tpl_dry_run": "Предпросмотр",
}

assert set(TEMPLATE_NAMES) == set(NOTIFICATION_TEMPLATE_LABELS.keys())


async def get_template_row(session: AsyncSession, name: str) -> NotificationTemplate | None:
    return await session.scalar(select(NotificationTemplate).where(NotificationTemplate.name == name))


async def list_all_templates(session: AsyncSession) -> list[NotificationTemplate]:
    r = await session.execute(select(NotificationTemplate).order_by(NotificationTemplate.name))
    return list(r.scalars().all())


async def upsert_template_body(
    session: AsyncSession,
    *,
    name: str,
    body_html: str | None,
    body_plain: str | None,
    updated_by: str | None,
) -> None:
    row = await get_template_row(session, name)
    if row is None:
        session.add(
            NotificationTemplate(
                name=name,
                body_html=body_html,
                body_plain=body_plain,
                updated_by=updated_by,
            )
        )
        return
    row.body_html = body_html
    row.body_plain = body_plain
    row.updated_by = updated_by


async def clear_override(session: AsyncSession, name: str) -> None:
    row = await get_template_row(session, name)
    if row is None:
        return
    row.body_html = None
    row.body_plain = None
