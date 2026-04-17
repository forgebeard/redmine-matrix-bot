"""Add role to redmine_statuses, is_active to redmine_statuses,
is_emergency to redmine_priorities.

Revision ID: 0019_status_role_emergency
Revises: 0018_pending_notifications_dlq
Create Date: 2026-04-14
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0019_status_role_emergency"
down_revision: str | None = "0018_pending_notifications_dlq"


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols_status = {c["name"] for c in insp.get_columns("redmine_statuses")}
    cols_prio = {c["name"] for c in insp.get_columns("redmine_priorities")}
    idx_status = {i["name"] for i in insp.get_indexes("redmine_statuses")}

    # 1. redmine_statuses: семантическая роль
    if "role" not in cols_status:
        op.add_column(
            "redmine_statuses",
            sa.Column("role", sa.String(64), nullable=True),
        )
    if "ix_redmine_statuses_role" not in idx_status:
        op.create_index(
            "ix_redmine_statuses_role",
            "redmine_statuses",
            ["role"],
            postgresql_where=sa.text("role IS NOT NULL"),
        )

    # 2. redmine_statuses: is_active (в миграции 0017 не было этого столбца)
    if "is_active" not in cols_status:
        op.add_column(
            "redmine_statuses",
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        )
    if "ix_redmine_statuses_is_active" not in idx_status:
        op.create_index(
            "ix_redmine_statuses_is_active",
            "redmine_statuses",
            ["is_active"],
        )

    # 3. redmine_priorities: is_emergency (пробивает DND)
    if "is_emergency" not in cols_prio:
        op.add_column(
            "redmine_priorities",
            sa.Column("is_emergency", sa.Boolean(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols_status = {c["name"] for c in insp.get_columns("redmine_statuses")}
    cols_prio = {c["name"] for c in insp.get_columns("redmine_priorities")}
    idx_status = {i["name"] for i in insp.get_indexes("redmine_statuses")}

    if "is_emergency" in cols_prio:
        op.drop_column("redmine_priorities", "is_emergency")
    if "ix_redmine_statuses_is_active" in idx_status:
        op.drop_index("ix_redmine_statuses_is_active", table_name="redmine_statuses")
    if "is_active" in cols_status:
        op.drop_column("redmine_statuses", "is_active")
    if "ix_redmine_statuses_role" in idx_status:
        op.drop_index("ix_redmine_statuses_role", table_name="redmine_statuses")
    if "role" in cols_status:
        op.drop_column("redmine_statuses", "role")
