"""add bulletin_drafts table

Revision ID: a1b2c3d4e5f6
Revises: f5a6b7c8d9e0
Create Date: 2026-06-15

"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # idempotent guard
    existing = [r[1] for r in op.get_bind().execute(
        sa.text("PRAGMA table_info(bulletin_drafts)")
    ).fetchall()]
    if existing:
        return

    op.create_table(
        "bulletin_drafts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("total_score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("note", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
    )
    op.create_index("ix_bulletin_drafts_status", "bulletin_drafts", ["status"])
    op.create_index("ix_bulletin_drafts_source", "bulletin_drafts", ["source"])


def downgrade() -> None:
    op.drop_table("bulletin_drafts")
