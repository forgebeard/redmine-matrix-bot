"""app auth: email -> login (поле входа, не почта)

Revision ID: 0008_admin_login
Revises: 0007_groups_ops
Create Date: 2026-04-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_admin_login"
down_revision: Union[str, None] = "0007_groups_ops"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "bot_app_users",
        "email",
        new_column_name="login",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.execute("ALTER INDEX ix_bot_app_users_email RENAME TO ix_bot_app_users_login")

    op.alter_column(
        "bot_magic_tokens",
        "email",
        new_column_name="login",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.execute("ALTER INDEX ix_bot_magic_tokens_email RENAME TO ix_bot_magic_tokens_login")

    op.alter_column(
        "password_reset_tokens",
        "requested_email",
        new_column_name="requested_login",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.execute(
        "ALTER INDEX ix_password_reset_tokens_requested_email RENAME TO ix_password_reset_tokens_requested_login"
    )


def downgrade() -> None:
    op.execute(
        "ALTER INDEX ix_password_reset_tokens_requested_login RENAME TO ix_password_reset_tokens_requested_email"
    )
    op.alter_column(
        "password_reset_tokens",
        "requested_login",
        new_column_name="requested_email",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )

    op.execute("ALTER INDEX ix_bot_magic_tokens_login RENAME TO ix_bot_magic_tokens_email")
    op.alter_column(
        "bot_magic_tokens",
        "login",
        new_column_name="email",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )

    op.execute("ALTER INDEX ix_bot_app_users_login RENAME TO ix_bot_app_users_email")
    op.alter_column(
        "bot_app_users",
        "login",
        new_column_name="email",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
