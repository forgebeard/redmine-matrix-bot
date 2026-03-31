"""support groups + bot ops audit + group link in bot_users

Revision ID: 0007_groups_ops
Revises: 0006_user_profile
Create Date: 2026-03-31
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007_groups_ops"
down_revision: Union[str, None] = "0006_user_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SEED_GROUPS: list[tuple[str, str]] = [
    ("Отдел технической поддержки РЕД ОС", ""),
    ("Отдел технической поддержки РЕД АДМ", ""),
    ("Отдел технической поддержки РЕД Базы Данных", ""),
    ("Отдел технической поддержки РЕД Виртуализации", ""),
    ("Отдел приема обращений", ""),
    ("Отдел развития технической поддержки системных и инфраструктурных продуктов", ""),
]
UNASSIGNED_NAME = "UNASSIGNED"


def upgrade() -> None:
    op.create_table(
        "support_groups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("room_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(op.f("ix_support_groups_name"), "support_groups", ["name"], unique=True)

    op.add_column("bot_users", sa.Column("group_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_bot_users_group_id"), "bot_users", ["group_id"], unique=False)
    op.create_foreign_key(
        "fk_bot_users_group_id_support_groups",
        "bot_users",
        "support_groups",
        ["group_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "bot_ops_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor_email", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bot_ops_audit_actor_email"), "bot_ops_audit", ["actor_email"], unique=False)
    op.create_index(op.f("ix_bot_ops_audit_action"), "bot_ops_audit", ["action"], unique=False)
    op.create_index(op.f("ix_bot_ops_audit_status"), "bot_ops_audit", ["status"], unique=False)
    op.create_index(op.f("ix_bot_ops_audit_created_at"), "bot_ops_audit", ["created_at"], unique=False)

    conn = op.get_bind()

    conn.execute(
        sa.text(
            "INSERT INTO support_groups (name, room_id, is_active) VALUES (:name, :room_id, true) "
            "ON CONFLICT (name) DO NOTHING"
        ),
        [{"name": UNASSIGNED_NAME, "room_id": ""}],
    )
    conn.execute(
        sa.text(
            "INSERT INTO support_groups (name, room_id, is_active) VALUES (:name, :room_id, true) "
            "ON CONFLICT (name) DO NOTHING"
        ),
        [{"name": name, "room_id": room_id} for name, room_id in SEED_GROUPS],
    )

    # Точное сопоставление legacy department -> support_groups.name.
    conn.execute(
        sa.text(
            """
            UPDATE bot_users u
            SET group_id = g.id
            FROM support_groups g
            WHERE u.department IS NOT NULL
              AND btrim(u.department) <> ''
              AND g.name = u.department
            """
        )
    )
    # Несопоставленные department уводим в UNASSIGNED.
    conn.execute(
        sa.text(
            """
            UPDATE bot_users
            SET group_id = (SELECT id FROM support_groups WHERE name = :name LIMIT 1)
            WHERE group_id IS NULL
              AND department IS NOT NULL
              AND btrim(department) <> ''
            """
        ),
        {"name": UNASSIGNED_NAME},
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_bot_ops_audit_created_at"), table_name="bot_ops_audit")
    op.drop_index(op.f("ix_bot_ops_audit_status"), table_name="bot_ops_audit")
    op.drop_index(op.f("ix_bot_ops_audit_action"), table_name="bot_ops_audit")
    op.drop_index(op.f("ix_bot_ops_audit_actor_email"), table_name="bot_ops_audit")
    op.drop_table("bot_ops_audit")

    op.drop_constraint("fk_bot_users_group_id_support_groups", "bot_users", type_="foreignkey")
    op.drop_index(op.f("ix_bot_users_group_id"), table_name="bot_users")
    op.drop_column("bot_users", "group_id")

    op.drop_index(op.f("ix_support_groups_name"), table_name="support_groups")
    op.drop_table("support_groups")
