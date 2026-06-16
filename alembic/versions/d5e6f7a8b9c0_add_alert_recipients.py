"""Add alert_recipients table for external trigger alert emails.

Revision ID: d5e6f7a8b9c0
Revises: b1c2d3e4f5a6
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = "d5e6f7a8b9c0"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Guard: skip if table already exists (idempotent)
    conn = op.get_bind()
    existing = [r[0] for r in conn.execute(sa.text("PRAGMA table_info(alert_recipients)")).fetchall()]
    if existing:
        return

    op.create_table(
        "alert_recipients",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(256), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.current_timestamp()),
    )
    op.create_index("ix_alert_recipients_email", "alert_recipients", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_alert_recipients_email", table_name="alert_recipients")
    op.drop_table("alert_recipients")
