"""add pending_notifications DLQ table

Revision ID: 20260413_0018_pending_notifications_dlq
Revises: 20260408_0017_reference_data
Create Date: 2026-04-13 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_pending_notifications_dlq"
down_revision: str | None = "0017_reference_data"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_redmine_id", sa.BigInteger(), nullable=False),
        sa.Column("issue_id", sa.BigInteger(), nullable=False),
        sa.Column("room_id", sa.Text(), nullable=False),
        sa.Column("notification_type", sa.String(50), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_pending_notifications_user_redmine_id"),
        "pending_notifications",
        ["user_redmine_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pending_notifications_issue_id"),
        "pending_notifications",
        ["issue_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pending_notifications_next_retry_at"),
        "pending_notifications",
        ["next_retry_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_pending_notifications_next_retry_at"),
        table_name="pending_notifications",
    )
    op.drop_index(
        op.f("ix_pending_notifications_issue_id"),
        table_name="pending_notifications",
    )
    op.drop_index(
        op.f("ix_pending_notifications_user_redmine_id"),
        table_name="pending_notifications",
    )
    op.drop_table("pending_notifications")
