"""add job_runs table

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    existing = [r[1] for r in conn.execute(sa.text("PRAGMA table_info(job_runs)")).fetchall()]
    if not existing:
        op.create_table(
            "job_runs",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("job_name", sa.String(64), nullable=False, index=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
            sa.Column("detail", sa.Text, nullable=False, server_default=""),
        )


def downgrade():
    op.drop_table("job_runs")
