"""add redmine statuses, versions, priorities and bot columns

Revision ID: fix_all_tables
Revises: 0018_pending_notifications_dlq
Create Date: 2026-04-15 12:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = 'fix_all_tables'
down_revision: str | None = '0018_pending_notifications_dlq'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create redmine_statuses if not exists
    op.create_table(
        'redmine_statuses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('redmine_status_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('is_closed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('redmine_status_id'),
    )
    op.create_index(op.f('ix_redmine_statuses_is_active'), 'redmine_statuses', ['is_active'], unique=False)
    op.create_index(op.f('ix_redmine_statuses_redmine_status_id'), 'redmine_statuses', ['redmine_status_id'], unique=True)

    # Create redmine_versions if not exists
    op.create_table(
        'redmine_versions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('redmine_version_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('redmine_version_id'),
    )
    op.create_index(op.f('ix_redmine_versions_is_active'), 'redmine_versions', ['is_active'], unique=False)
    op.create_index(op.f('ix_redmine_versions_redmine_version_id'), 'redmine_versions', ['redmine_version_id'], unique=True)

    # Create redmine_priorities if not exists
    op.create_table(
        'redmine_priorities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('redmine_priority_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('redmine_priority_id'),
    )
    op.create_index(op.f('ix_redmine_priorities_is_active'), 'redmine_priorities', ['is_active'], unique=False)
    op.create_index(op.f('ix_redmine_priorities_redmine_priority_id'), 'redmine_priorities', ['redmine_priority_id'], unique=True)

    # Add columns to bot_users if not exists
    op.add_column('bot_users', sa.Column('versions', sa.JSON(), nullable=False, server_default='["all"]'))
    op.add_column('bot_users', sa.Column('priorities', sa.JSON(), nullable=False, server_default='["all"]'))

    # Add columns to support_groups if not exists
    op.add_column('support_groups', sa.Column('versions', sa.JSON(), nullable=False, server_default='["all"]'))
    op.add_column('support_groups', sa.Column('priorities', sa.JSON(), nullable=False, server_default='["all"]'))


def downgrade() -> None:
    op.drop_column('support_groups', 'priorities')
    op.drop_column('support_groups', 'versions')
    op.drop_column('bot_users', 'priorities')
    op.drop_column('bot_users', 'versions')
    op.drop_index(op.f('ix_redmine_priorities_redmine_priority_id'), table_name='redmine_priorities')
    op.drop_index(op.f('ix_redmine_priorities_is_active'), table_name='redmine_priorities')
    op.drop_table('redmine_priorities')
    op.drop_index(op.f('ix_redmine_versions_redmine_version_id'), table_name='redmine_versions')
    op.drop_index(op.f('ix_redmine_versions_is_active'), table_name='redmine_versions')
    op.drop_table('redmine_versions')
    op.drop_index(op.f('ix_redmine_statuses_redmine_status_id'), table_name='redmine_statuses')
    op.drop_index(op.f('ix_redmine_statuses_is_active'), table_name='redmine_statuses')
    op.drop_table('redmine_statuses')
