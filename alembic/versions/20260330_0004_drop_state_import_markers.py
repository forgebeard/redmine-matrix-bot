"""drop obsolete bot_state_import_markers table

Revision ID: 0004_drop_state_import_markers
Revises: 0003_auth_matrix_bindings
Create Date: 2026-03-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_drop_state_import_markers"
down_revision: Union[str, None] = "0003_auth_matrix_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Таблица была нужна только для одноразового импорта JSON-state.
    # В DB-only режиме она не используется, удаляем безопасно.
    op.execute("DROP TABLE IF EXISTS bot_state_import_markers")


def downgrade() -> None:
    # Возвращаем таблицу для обратной совместимости со старой цепочкой миграций.
    op.create_table(
        "bot_state_import_markers",
        sa.Column("marker_name", sa.String(length=64), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("marker_name"),
    )
