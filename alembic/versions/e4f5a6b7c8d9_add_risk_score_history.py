"""add risk_score_history table

Revision ID: e4f5a6b7c8d9
Revises: d4e5f6a7b8c9
Create Date: 2026-06-12
"""
from alembic import op
import sqlalchemy as sa

revision = "e4f5a6b7c8d9"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    existing = [r[1] for r in conn.execute(sa.text("PRAGMA table_info(risk_score_history)")).fetchall()]
    if not existing:
        op.create_table(
            "risk_score_history",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source", sa.String(64), nullable=False, server_default="CHIRPS"),
            sa.Column("total", sa.Integer, nullable=False),
            sa.Column("level", sa.String(16), nullable=False),
            sa.Column("spi_pts", sa.Integer, nullable=False),
            sa.Column("seasonal_pts", sa.Integer, nullable=False),
            sa.Column("trigger_pts", sa.Integer, nullable=False),
            sa.Column("worst_spi", sa.Float, nullable=True),
        )


def downgrade():
    op.drop_table("risk_score_history")
