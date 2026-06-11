"""add seasonal_forecasts table

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "seasonal_forecasts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("issue_date", sa.Date, nullable=False),
        sa.Column("valid_start", sa.Date, nullable=False),
        sa.Column("valid_end", sa.Date, nullable=False),
        sa.Column("variable", sa.String(16), nullable=False, server_default="precip"),
        sa.Column("below_normal_pct", sa.Float, nullable=True),
        sa.Column("near_normal_pct", sa.Float, nullable=True),
        sa.Column("above_normal_pct", sa.Float, nullable=True),
        sa.Column("precip_anomaly_pct", sa.Float, nullable=True),
        sa.Column("region_label", sa.String(128), nullable=True),
        sa.Column("lat_min", sa.Float, nullable=True),
        sa.Column("lat_max", sa.Float, nullable=True),
        sa.Column("lon_min", sa.Float, nullable=True),
        sa.Column("lon_max", sa.Float, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "uploaded_by_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_table("seasonal_forecasts")
