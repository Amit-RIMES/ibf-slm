"""Add webhook_deliveries table

Revision ID: l6m7n8o9p0q1
Revises: k5l6m7n8o9p0
Create Date: 2026-06-18 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "l6m7n8o9p0q1"
down_revision: Union[str, None] = "k5l6m7n8o9p0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    ).fetchall()
    return len(rows) > 0


def upgrade() -> None:
    if not _table_exists("webhook_deliveries"):
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("webhook_id", sa.Integer(), sa.ForeignKey("webhooks.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("activation_id", sa.Integer(), nullable=True, index=True),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("status_code", sa.Integer(), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("success", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
