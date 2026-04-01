"""Удаление организационно-специфичных сидов support_groups (универсальный продукт).

Revision ID: 0009_drop_legacy_org_seeds
Revises: 0008_admin_login
Create Date: 2026-04-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_drop_legacy_org_seeds"
down_revision: Union[str, None] = "0008_admin_login"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Имена, ранее вставлявшиеся в 0007_groups_ops; на bot_users.group_id — ON DELETE SET NULL.
_LEGACY_SEED_NAMES: tuple[str, ...] = (
    "Отдел технической поддержки РЕД ОС",
    "Отдел технической поддержки РЕД АДМ",
    "Отдел технической поддержки РЕД Базы Данных",
    "Отдел технической поддержки РЕД Виртуализации",
    "Отдел приема обращений",
    "Отдел развития технической поддержки системных и инфраструктурных продуктов",
)


def upgrade() -> None:
    conn = op.get_bind()
    stmt = sa.text("DELETE FROM support_groups WHERE name = :name")
    for name in _LEGACY_SEED_NAMES:
        conn.execute(stmt, {"name": name})


def downgrade() -> None:
    # Не восстанавливаем организационно-специфичные строки.
    pass
