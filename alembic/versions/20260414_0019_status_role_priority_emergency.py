"""Add role to redmine_statuses, is_active to redmine_statuses,
is_emergency to redmine_priorities.

Revision ID: 0019_status_role_priority_emergency
Revises: 0018_pending_notifications_dlq
Create Date: 2026-04-14
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0019_status_role_priority_emergency"
down_revision: str | None = "0018_pending_notifications_dlq"


def upgrade() -> None:
    # 1. redmine_statuses: семантическая роль
    op.add_column(
        "redmine_statuses",
        sa.Column("role", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_redmine_statuses_role",
        "redmine_statuses",
        ["role"],
        postgresql_where=sa.text("role IS NOT NULL"),
    )

    # 2. redmine_statuses: is_active (в миграции 0017 не было этого столбца)
    op.add_column(
        "redmine_statuses",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.create_index(
        "ix_redmine_statuses_is_active",
        "redmine_statuses",
        ["is_active"],
    )

    # 3. redmine_priorities: is_emergency (пробивает DND)
    op.add_column(
        "redmine_priorities",
        sa.Column("is_emergency", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("redmine_priorities", "is_emergency")
    op.drop_index("ix_redmine_statuses_is_active", table_name="redmine_statuses")
    op.drop_column("redmine_statuses", "is_active")
    op.drop_index("ix_redmine_statuses_role", table_name="redmine_statuses")
    op.drop_column("redmine_statuses", "role")
