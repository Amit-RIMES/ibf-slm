"""Add observed_rainfall table for CHIRPS ingestion

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-06-08 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # Check table does not already exist (idempotent)
    tables = [r[0] for r in conn.execute(sa.text(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='observed_rainfall'"
    )).fetchall()]

    if "observed_rainfall" not in tables:
        op.create_table(
            "observed_rainfall",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("obs_date", sa.Date, nullable=False),
            sa.Column("source", sa.String(32), nullable=False, server_default="CHIRPS"),
            sa.Column("lat_min", sa.Float, nullable=False),
            sa.Column("lat_max", sa.Float, nullable=False),
            sa.Column("lon_min", sa.Float, nullable=False),
            sa.Column("lon_max", sa.Float, nullable=False),
            sa.Column("precip_mean", sa.Float, nullable=False),
            sa.Column("precip_max", sa.Float, nullable=False),
            sa.Column("precip_min", sa.Float, nullable=False),
            sa.Column("wet_fraction", sa.Float, nullable=False),
            sa.Column("pixel_count", sa.Integer, nullable=False),
            sa.Column("is_preliminary", sa.Boolean, nullable=False, server_default="0"),
            sa.Column("geojson", sa.Text, nullable=True),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Create index only if it doesn't exist
    indexes = [r[0] for r in conn.execute(sa.text(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='observed_rainfall'"
    )).fetchall()]

    if "ix_observed_rainfall_obs_date" not in indexes:
        op.create_index("ix_observed_rainfall_obs_date", "observed_rainfall", ["obs_date"])
    # uq_obs_date_source is defined in the CREATE TABLE statement (SQLite can't ALTER to add it)


def downgrade():
    op.drop_table("observed_rainfall")
