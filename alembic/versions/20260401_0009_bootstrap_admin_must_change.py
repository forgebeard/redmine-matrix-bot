"""bot_app_users.must_change_credentials + встроенный admin/admin при пустой БД

Revision ID: 0009_bootstrap_admin
Revises: 0008_login_auth
Create Date: 2026-04-01
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_bootstrap_admin"
down_revision: Union[str, None] = "0008_login_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "bot_app_users",
        sa.Column("must_change_credentials", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.alter_column("bot_app_users", "must_change_credentials", server_default=None)

    _root = Path(__file__).resolve().parents[2]
    _src = str(_root / "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from security import hash_password

    bind = op.get_bind()
    cnt = bind.execute(sa.text("SELECT COUNT(*) FROM bot_app_users WHERE role = 'admin'")).scalar()
    if int(cnt or 0) == 0:
        uid = uuid.uuid4()
        ph = hash_password("admin")
        bind.execute(
            sa.text(
                """
                INSERT INTO bot_app_users (
                    id, login, email, role, verified_at, redmine_id,
                    password_hash, session_version, must_change_credentials
                )
                VALUES (
                    :id, 'admin', NULL, 'admin', NOW(), NULL,
                    :ph, 1, true
                )
                """
            ),
            {"id": uid, "ph": ph},
        )


def downgrade() -> None:
    op.drop_column("bot_app_users", "must_change_credentials")
