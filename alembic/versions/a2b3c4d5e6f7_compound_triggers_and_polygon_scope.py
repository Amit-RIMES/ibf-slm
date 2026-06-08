"""Add compound trigger conditions and polygon scope

Revision ID: a2b3c4d5e6f7
Revises: f8b4d2e9a3c1
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa

revision = "a2b3c4d5e6f7"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    existing = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(triggers)")).fetchall()}

    with op.batch_alter_table("triggers") as batch_op:
        if "condition_2_variable" not in existing:
            batch_op.add_column(sa.Column("condition_2_variable", sa.String(64), nullable=True))
        if "condition_2_operator" not in existing:
            batch_op.add_column(sa.Column("condition_2_operator", sa.String(8), nullable=True))
        if "condition_2_threshold" not in existing:
            batch_op.add_column(sa.Column("condition_2_threshold", sa.Float(), nullable=True))
        if "logic_op" not in existing:
            batch_op.add_column(
                sa.Column("logic_op", sa.String(8), nullable=False, server_default="and")
            )
        if "scope_polygon" not in existing:
            batch_op.add_column(sa.Column("scope_polygon", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("triggers") as batch_op:
        batch_op.drop_column("scope_polygon")
        batch_op.drop_column("logic_op")
        batch_op.drop_column("condition_2_threshold")
        batch_op.drop_column("condition_2_operator")
        batch_op.drop_column("condition_2_variable")
