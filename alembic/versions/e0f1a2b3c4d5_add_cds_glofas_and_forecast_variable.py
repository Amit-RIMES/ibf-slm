"""Add CDS config, GloFAS records, and variable fields (Batch 19)

Revision ID: e0f1a2b3c4d5
Revises: c0d1e2f3a4b5
Create Date: 2026-06-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e0f1a2b3c4d5"
down_revision: Union[str, None] = "c0d1e2f3a4b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add variable column to forecast_uploads
    op.add_column(
        "forecast_uploads",
        sa.Column("variable", sa.String(16), nullable=True, server_default="tp"),
    )

    # Add parameters column to ecmwf_config
    op.add_column(
        "ecmwf_config",
        sa.Column("parameters", sa.String(256), nullable=True, server_default='["tp"]'),
    )

    # Create cds_config table
    op.create_table(
        "cds_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("api_key", sa.String(256), nullable=True),
        sa.Column("api_url", sa.String(256), nullable=False,
                  server_default="https://cds.climate.copernicus.eu/api/v2"),
        sa.Column("lat_min", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("lat_max", sa.Float(), nullable=False, server_default="35.0"),
        sa.Column("lon_min", sa.Float(), nullable=False, server_default="60.0"),
        sa.Column("lon_max", sa.Float(), nullable=False, server_default="155.0"),
        # SEAS5
        sa.Column("seas5_enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("seas5_sync_hour", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("seas5_sync_minute", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("seas5_lead_months", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("seas5_last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("seas5_last_run_status", sa.String(16), nullable=True),
        sa.Column("seas5_last_run_detail", sa.String(512), nullable=True),
        # ERA5
        sa.Column("era5_enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("era5_sync_hour", sa.Integer(), nullable=False, server_default="9"),
        sa.Column("era5_sync_minute", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("era5_lookback_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("era5_last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("era5_last_run_status", sa.String(16), nullable=True),
        sa.Column("era5_last_run_detail", sa.String(512), nullable=True),
        # GloFAS
        sa.Column("glofas_enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("glofas_sync_hour", sa.Integer(), nullable=False, server_default="11"),
        sa.Column("glofas_sync_minute", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("glofas_last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("glofas_last_run_status", sa.String(16), nullable=True),
        sa.Column("glofas_last_run_detail", sa.String(512), nullable=True),
    )

    # Create glofas_records table
    op.create_table(
        "glofas_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("forecast_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default="GloFAS-v4"),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat_min", sa.Float(), nullable=False),
        sa.Column("lat_max", sa.Float(), nullable=False),
        sa.Column("lon_min", sa.Float(), nullable=False),
        sa.Column("lon_max", sa.Float(), nullable=False),
        sa.Column("discharge_min", sa.Float(), nullable=False),
        sa.Column("discharge_max", sa.Float(), nullable=False),
        sa.Column("discharge_mean", sa.Float(), nullable=False),
        sa.Column("lead_days", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("geojson", sa.Text(), nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_glofas_records_forecast_date", "glofas_records", ["forecast_date"])


def downgrade() -> None:
    op.drop_index("ix_glofas_records_forecast_date", "glofas_records")
    op.drop_table("glofas_records")
    op.drop_table("cds_config")
    op.drop_column("ecmwf_config", "parameters")
    op.drop_column("forecast_uploads", "variable")
