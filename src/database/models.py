"""ORM-модели конфигурации и state бота (Postgres).

Config:
  - BotUser, StatusRoomRoute, VersionRoomRoute

State:
  - BotUserLease: координация обработки пользователя несколькими инстансами
  - BotIssueState: дедупликация и таймеры уведомлений (sent/reminders/overdue/journals)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class BotUser(Base):
    __tablename__ = "bot_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    redmine_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    group_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("support_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    department: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    room: Mapped[str] = mapped_column(Text, nullable=False)
    notify: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["all"])  # Statuses
    versions: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["all"])
    priorities: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["all"])
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    work_hours: Mapped[str | None] = mapped_column(String(32), nullable=True)
    work_days: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    dnd: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class StatusRoomRoute(Base):
    __tablename__ = "status_room_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False, index=True)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notify_on_assignment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class VersionRoomRoute(Base):
    __tablename__ = "version_room_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False, index=True)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notify_on_assignment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class GroupVersionRoute(Base):
    """Версия Redmine (подстрока в названии версии задачи) → Matrix-комната для группы."""

    __tablename__ = "group_version_routes"
    __table_args__ = (
        UniqueConstraint("group_id", "version_key", name="uq_group_version_routes_group_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("support_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_key: Mapped[str] = mapped_column(String(512), nullable=False)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notify_on_assignment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class UserVersionRoute(Base):
    """Версия Redmine → Matrix-комната для пользователя бота (личные доп. маршруты)."""

    __tablename__ = "user_version_routes"
    __table_args__ = (
        UniqueConstraint("bot_user_id", "version_key", name="uq_user_version_routes_user_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot_user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("bot_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_key: Mapped[str] = mapped_column(String(512), nullable=False)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notify_on_assignment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SupportGroup(Base):
    __tablename__ = "support_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)
    notify_on_assignment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notify: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["all"])  # Statuses
    versions: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["all"])
    priorities: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["all"])
    work_hours: Mapped[str | None] = mapped_column(String(32), nullable=True)
    work_days: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    dnd: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BotOpsAudit(Base):
    __tablename__ = "bot_ops_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_login: Mapped[str | None] = mapped_column(
        "actor_email", String(255), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Заполняются для action=ADMIN_CRUD (остальные строки — Docker и т.д.)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    crud_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    details_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Dead-letter queue — уведомления, не доставленные в Matrix
# ═══════════════════════════════════════════════════════════════════════════


class PendingNotification(Base):
    __tablename__ = "pending_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_redmine_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    issue_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)
    notification_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ═══════════════════════════════════════════════════════════════════════════
# State и координация для DB-only режима
# ═══════════════════════════════════════════════════════════════════════════


class BotUserLease(Base):
    __tablename__ = "bot_user_leases"

    # Если несколько инстансов бота одновременно, lease гарантирует эксклюзивное
    # владение обработкой для данного user_redmine_id (напр. задачи без исполнителя).
    user_redmine_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, nullable=False)
    lease_owner_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BotIssueState(Base):
    __tablename__ = "bot_issue_state"

    user_redmine_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    issue_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_notified_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # journals: последний journal_id (legacy; глобальный курсор — bot_issue_journal_cursor)
    last_journal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    group_reminder_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    personal_reminder_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminder_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # reminders / overdue таймеры
    last_reminder_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_overdue_notified_at: Mapped[str | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BotIssueJournalCursor(Base):
    """Глобальный курсор по журналам задачи (одна строка на issue_id)."""

    __tablename__ = "bot_issue_journal_cursor"

    issue_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_journal_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PendingDigest(Base):
    __tablename__ = "pending_digests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bot_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    issue_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    issue_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    journal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    journal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    assigned_to: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BotWatcherCache(Base):
    __tablename__ = "bot_watcher_cache"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bot_users.id", ondelete="CASCADE"), primary_key=True
    )
    issue_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_plain: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)


# ═══════════════════════════════════════════════════════════════════════════
# Auth панели: логин + пароль (+ RBAC)
# ═══════════════════════════════════════════════════════════════════════════


class BotAppUser(Base):
    __tablename__ = "bot_app_users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    login: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Связь с редмайн-пользователем для self-service.
    redmine_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class BotMagicToken(Base):
    __tablename__ = "bot_magic_tokens"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    login: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BotSession(Base):
    __tablename__ = "bot_sessions"

    session_token: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    requested_login: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AppSecret(Base):
    __tablename__ = "app_secrets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ═══════════════════════════════════════════════════════════════════════════
# Справочники и настройки (GUI-управление)
# ═══════════════════════════════════════════════════════════════════════════


class RedmineStatus(Base):
    """Справочник статусов Redmine (загружается из API через админку)."""

    __tablename__ = "redmine_statuses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    redmine_status_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Семантическая роль — назначается админом в UI.
    # Допустимые: "trigger_new", "trigger_info_provided",
    #             "trigger_reopened", "trigger_transferred", None
    role: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RedmineVersion(Base):
    """Справочник версий Redmine."""

    __tablename__ = "redmine_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    redmine_version_id: Mapped[int] = mapped_column(
        Integer, unique=True, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RedminePriority(Base):
    """Справочник приоритетов Redmine (загружается из API через админку)."""

    __tablename__ = "redmine_priorities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    redmine_priority_id: Mapped[int] = mapped_column(
        Integer, unique=True, nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Пробивает DND (аварийный приоритет) — назначается админом
    is_emergency: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class NotificationType(Base):
    """Типы уведомлений (эмодзи, название, ключ)."""

    __tablename__ = "notification_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    emoji: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CycleSettings(Base):
    """Настройки цикла опроса бота (интервалы, таймауты)."""

    __tablename__ = "cycle_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ═══════════════════════════════════════════════════════════════════════════
# Heartbeat бота (мониторинг живучести)
# ═══════════════════════════════════════════════════════════════════════════


class BotHeartbeat(Base):
    __tablename__ = "bot_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Уникальная запись для одного инстанса бота.
    instance_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), unique=True, nullable=False, index=True
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ═══════════════════════════════════════════════════════════════════════════
# Matrix room binding (one-time code → binding + bot_users.room update)
# ═══════════════════════════════════════════════════════════════════════════


class MatrixRoomBinding(Base):
    __tablename__ = "matrix_room_bindings"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    redmine_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)

    # Одноразовый код для подтверждения.
    verify_code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
