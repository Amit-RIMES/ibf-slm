"""add ecmwf_config

Revision ID: c0d1e2f3a4b5
Revises: f8b4d2e9a3c1
Create Date: 2026-06-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c0d1e2f3a4b5"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ecmwf_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("use_ensemble", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("run_time", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sync_hour", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("sync_minute", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lat_min", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("lat_max", sa.Float(), nullable=False, server_default="35.0"),
        sa.Column("lon_min", sa.Float(), nullable=False, server_default="60.0"),
        sa.Column("lon_max", sa.Float(), nullable=False, server_default="155.0"),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_status", sa.String(16), nullable=True),
        sa.Column("last_run_detail", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ecmwf_config")
