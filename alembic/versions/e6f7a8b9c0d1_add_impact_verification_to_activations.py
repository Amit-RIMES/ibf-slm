"""Add impact verification fields to trigger_activations.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    existing_cols = [r[1] for r in conn.execute(
        sa.text("PRAGMA table_info(trigger_activations)")
    ).fetchall()]

    if "impact_verdict" not in existing_cols:
        op.add_column("trigger_activations",
                      sa.Column("impact_verdict", sa.String(16), nullable=True))
    if "impact_notes" not in existing_cols:
        op.add_column("trigger_activations",
                      sa.Column("impact_notes", sa.Text, nullable=True))
    if "verified_at" not in existing_cols:
        op.add_column("trigger_activations",
                      sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # SQLite doesn't support DROP COLUMN on older versions; use recreate pattern
    with op.batch_alter_table("trigger_activations") as batch_op:
        batch_op.drop_column("verified_at")
        batch_op.drop_column("impact_notes")
        batch_op.drop_column("impact_verdict")
