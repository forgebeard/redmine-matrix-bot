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
    notify: Mapped[list] = mapped_column(JSONB, nullable=False, default=lambda: ["all"])
    work_hours: Mapped[str | None] = mapped_column(String(32), nullable=True)
    work_days: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    dnd: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class StatusRoomRoute(Base):
    __tablename__ = "status_room_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False, index=True)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)


class VersionRoomRoute(Base):
    __tablename__ = "version_room_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False, index=True)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)


class SupportGroup(Base):
    __tablename__ = "support_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    room_id: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


# ═══════════════════════════════════════════════════════════════════════════
# State и координация для DB-only режима
# ═══════════════════════════════════════════════════════════════════════════


class BotUserLease(Base):
    __tablename__ = "bot_user_leases"

    # Если несколько инстансов бота одновременно, lease гарантирует:
    # один инстанс выполняет check_user_issues(user) в рамках одного цикла.
    user_redmine_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, nullable=False
    )
    lease_owner_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BotIssueState(Base):
    __tablename__ = "bot_issue_state"

    user_redmine_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    issue_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_notified_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # journals: последний journal_id, чтобы определять issue_updated
    last_journal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # reminders / overdue таймеры
    last_reminder_at: Mapped[str | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_overdue_notified_at: Mapped[str | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


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

    session_token: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True
    )
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
