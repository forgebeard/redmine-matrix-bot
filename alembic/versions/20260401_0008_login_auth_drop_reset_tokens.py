"""bot_app_users.login + deprecate email; drop password_reset_tokens

Revision ID: 0008_login_auth
Revises: 0007_groups_ops
Create Date: 2026-04-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0008_login_auth"
down_revision: Union[str, None] = "0007_groups_ops"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("bot_app_users", sa.Column("login", sa.String(length=255), nullable=True))
    op.execute(
        sa.text("UPDATE bot_app_users SET login = lower(trim(email)) WHERE email IS NOT NULL AND login IS NULL")
    )
    op.alter_column("bot_app_users", "login", nullable=False)
    op.create_index(op.f("ix_bot_app_users_login"), "bot_app_users", ["login"], unique=True)

    op.drop_index(op.f("ix_bot_app_users_email"), table_name="bot_app_users")
    op.alter_column("bot_app_users", "email", existing_type=sa.String(length=255), nullable=True)

    # Таблица могла быть удалена внешней/промежуточной миграцией — не падаем на DROP.
    op.execute(sa.text("DROP TABLE IF EXISTS password_reset_tokens CASCADE"))


def downgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("requested_email", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        op.f("ix_password_reset_tokens_user_id"),
        "password_reset_tokens",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_password_reset_tokens_requested_email"),
        "password_reset_tokens",
        ["requested_email"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_password_reset_tokens_token_hash",
        "password_reset_tokens",
        ["token_hash"],
    )

    op.execute(sa.text("UPDATE bot_app_users SET email = login WHERE email IS NULL"))
    op.alter_column("bot_app_users", "email", existing_type=sa.String(length=255), nullable=False)
    op.create_index(op.f("ix_bot_app_users_email"), "bot_app_users", ["email"], unique=True)

    op.drop_index(op.f("ix_bot_app_users_login"), table_name="bot_app_users")
    op.drop_column("bot_app_users", "login")
