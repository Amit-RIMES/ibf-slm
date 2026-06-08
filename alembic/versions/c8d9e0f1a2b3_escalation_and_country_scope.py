"""Add last_escalated_at to trigger_activations and country_scope to users

Revision ID: c8d9e0f1a2b3
Revises: a2b3c4d5e6f7
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa

revision = "c8d9e0f1a2b3"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    ta_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(trigger_activations)")).fetchall()}
    with op.batch_alter_table("trigger_activations") as batch_op:
        if "last_escalated_at" not in ta_cols:
            batch_op.add_column(sa.Column("last_escalated_at", sa.DateTime(timezone=True), nullable=True))

    u_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(users)")).fetchall()}
    with op.batch_alter_table("users") as batch_op:
        if "country_scope" not in u_cols:
            batch_op.add_column(sa.Column("country_scope", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("country_scope")
    with op.batch_alter_table("trigger_activations") as batch_op:
        batch_op.drop_column("last_escalated_at")
