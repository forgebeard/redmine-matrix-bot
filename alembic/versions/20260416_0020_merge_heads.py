"""Merge parallel heads after 0018.

Revision ID: 0020_merge_heads
Revises: 0019_status_role_emergency, fix_all_tables
Create Date: 2026-04-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_merge_heads"
down_revision: str | Sequence[str] | None = (
    "0019_status_role_emergency",
    "fix_all_tables",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Merge revision only (no schema changes).
    pass


def downgrade() -> None:
    # Merge revision only (no schema changes).
    pass
