"""Загрузка справочников из БД.

Бот = руки. Никаких собственных решений и fallback-значений.
Если справочник пуст — бот логирует ошибку и работает с пустыми множествами.
Админ обязан заполнить справочники через UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    CycleSettings,
    NotificationType,
    RedminePriority,
    RedmineStatus,
)
from database.session import get_session_factory

logger = logging.getLogger("redmine_bot")


# Допустимые значения RedmineStatus.role
VALID_STATUS_ROLES = frozenset({
    "trigger_new",
    "trigger_info_provided",
    "trigger_reopened",
    "trigger_transferred",
})


@dataclass(frozen=True)
class BotCatalogs:
    """Все справочники из БД. Передаётся в processor/sender/scheduler."""

    # ── Статусы ──────────────────────────────────────────────────────
    status_id_to_name: dict[int, str] = field(default_factory=dict)
    status_name_to_id: dict[str, int] = field(default_factory=dict)

    trigger_new_ids: frozenset[int] = field(default_factory=frozenset)
    trigger_info_provided_ids: frozenset[int] = field(default_factory=frozenset)
    trigger_reopened_ids: frozenset[int] = field(default_factory=frozenset)
    trigger_transferred_ids: frozenset[int] = field(default_factory=frozenset)
    closed_status_ids: frozenset[int] = field(default_factory=frozenset)

    # ── Приоритеты ───────────────────────────────────────────────────
    priority_id_to_name: dict[int, str] = field(default_factory=dict)
    priority_name_to_id: dict[str, int] = field(default_factory=dict)
    emergency_priority_names: frozenset[str] = field(default_factory=frozenset)
    emergency_priority_ids: frozenset[int] = field(default_factory=frozenset)

    # ── Типы уведомлений ─────────────────────────────────────────────
    notification_types: dict[str, tuple[str, str]] = field(default_factory=dict)

    # ── Настройки цикла ──────────────────────────────────────────────
    cycle_settings: dict[str, str] = field(default_factory=dict)

    # ── Хелперы ──────────────────────────────────────────────────────

    def status_name(self, redmine_id: int, default: str = "?") -> str:
        return self.status_id_to_name.get(redmine_id, default)

    def priority_name(self, redmine_id: int, default: str = "?") -> str:
        return self.priority_id_to_name.get(redmine_id, default)

    def notification_emoji_label(self, key: str) -> tuple[str, str]:
        return self.notification_types.get(key, ("🔔", key))

    def cycle_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.cycle_settings.get(key, str(default)))
        except (ValueError, TypeError):
            return default

    def is_emergency(self, priority_name: str | None = None, priority_id: int | None = None) -> bool:
        if priority_name is not None:
            return priority_name in self.emergency_priority_names
        if priority_id is not None:
            return priority_id in self.emergency_priority_ids
        return False


async def load_catalogs(session: AsyncSession | None = None) -> BotCatalogs:
    """Загружает все справочники из БД. Если пусто — пустые структуры + warning."""
    if session is None:
        factory = get_session_factory()
        async with factory() as s:
            return await load_catalogs(s)

    # ── 1. Статусы ───────────────────────────────────────────────────
    rows = await session.execute(
        select(RedmineStatus).where(RedmineStatus.is_active.is_(True))
    )
    statuses = list(rows.scalars().all())

    if not statuses:
        logger.error("❌ Таблица redmine_statuses пуста! Заполните справочники через админку.")

    status_id_to_name = {s.redmine_status_id: s.name for s in statuses}
    status_name_to_id = {s.name: s.redmine_status_id for s in statuses}

    role_map: dict[str, set[int]] = {r: set() for r in VALID_STATUS_ROLES}
    for s in statuses:
        if s.role and s.role in VALID_STATUS_ROLES:
            role_map[s.role].add(s.redmine_status_id)

    closed_ids = frozenset(s.redmine_status_id for s in statuses if s.is_closed)

    roles_assigned = sum(len(v) for v in role_map.values())
    if statuses and roles_assigned == 0:
        logger.warning(
            "⚠ Статусы загружены (%d), но ни одному не назначена роль. "
            "Назначьте роли в админке → Справочники → Статусы.",
            len(statuses),
        )

    logger.info(
        "📋 Статусы: %d (new=%d, info=%d, reopen=%d, transfer=%d, closed=%d)",
        len(statuses),
        len(role_map["trigger_new"]),
        len(role_map["trigger_info_provided"]),
        len(role_map["trigger_reopened"]),
        len(role_map["trigger_transferred"]),
        len(closed_ids),
    )

    # ── 2. Приоритеты ────────────────────────────────────────────────
    rows = await session.execute(
        select(RedminePriority).where(RedminePriority.is_active.is_(True))
    )
    priorities = list(rows.scalars().all())

    if not priorities:
        logger.error("❌ Таблица redmine_priorities пуста! Заполните справочники через админку.")

    priority_id_to_name = {p.redmine_priority_id: p.name for p in priorities}
    priority_name_to_id = {p.name: p.redmine_priority_id for p in priorities}
    emergency_names = frozenset(p.name for p in priorities if p.is_emergency)
    emergency_ids = frozenset(p.redmine_priority_id for p in priorities if p.is_emergency)

    logger.info(
        "📋 Приоритеты: %d (emergency=%d)",
        len(priorities), len(emergency_ids),
    )

    # ── 3. Типы уведомлений ──────────────────────────────────────────
    rows = await session.execute(
        select(NotificationType)
        .where(NotificationType.is_active.is_(True))
        .order_by(NotificationType.sort_order)
    )
    ntypes = list(rows.scalars().all())

    if not ntypes:
        logger.error("❌ Таблица notification_types пуста! Заполните справочники через админку.")

    notification_types = {nt.key: (nt.emoji, nt.label) for nt in ntypes}
    logger.info("📋 Типы уведомлений: %d", len(ntypes))

    # ── 4. Настройки цикла ───────────────────────────────────────────
    rows = await session.execute(select(CycleSettings))
    csettings = list(rows.scalars().all())

    cycle_settings = {cs.key: cs.value for cs in csettings}
    logger.info("📋 Настройки цикла: %d", len(cycle_settings))

    # ── Результат ────────────────────────────────────────────────────
    return BotCatalogs(
        status_id_to_name=status_id_to_name,
        status_name_to_id=status_name_to_id,
        trigger_new_ids=frozenset(role_map["trigger_new"]),
        trigger_info_provided_ids=frozenset(role_map["trigger_info_provided"]),
        trigger_reopened_ids=frozenset(role_map["trigger_reopened"]),
        trigger_transferred_ids=frozenset(role_map["trigger_transferred"]),
        closed_status_ids=closed_ids,
        priority_id_to_name=priority_id_to_name,
        priority_name_to_id=priority_name_to_id,
        emergency_priority_names=emergency_names,
        emergency_priority_ids=emergency_ids,
        notification_types=notification_types,
        cycle_settings=cycle_settings,
    )
