"""add spi_records table

Revision ID: a1b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check idempotently — SQLite doesn't support IF NOT EXISTS on indexes
    conn = op.get_bind()
    tables = [r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()]
    if "spi_records" in tables:
        return

    op.create_table(
        "spi_records",
        sa.Column("id", sa.Integer, primary_key=True, index=True),
        sa.Column("source", sa.String(32), nullable=False, server_default="CHIRPS"),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("month", sa.Integer, nullable=False),
        sa.Column("timescale", sa.Integer, nullable=False),
        sa.Column("monthly_precip_mm", sa.Float, nullable=False),
        sa.Column("n_days", sa.Integer, nullable=False),
        sa.Column("spi_value", sa.Float, nullable=True),
        sa.Column("n_reference", sa.Integer, nullable=False, server_default="0"),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source", "year", "month", "timescale",
            name="uq_spi_source_year_month_scale",
        ),
    )
    op.create_index("ix_spi_source_timescale", "spi_records", ["source", "timescale"])


def downgrade() -> None:
    op.drop_index("ix_spi_source_timescale", table_name="spi_records")
    op.drop_table("spi_records")
