"""Station and StationObservation tables

Revision ID: k5l6m7n8o9p0
Revises: j4k5l6m7n8o9
Create Date: 2026-06-17 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "k5l6m7n8o9p0"
down_revision: Union[str, None] = "j4k5l6m7n8o9"
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
    if not _table_exists("stations"):
        op.create_table(
            "stations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("station_id", sa.String(64), nullable=False, unique=True),
            sa.Column("name", sa.String(128), nullable=False, server_default=""),
            sa.Column("country", sa.String(64), nullable=True),
            sa.Column("lat", sa.Float(), nullable=False),
            sa.Column("lon", sa.Float(), nullable=False),
            sa.Column("elevation_m", sa.Float(), nullable=True),
            sa.Column("source", sa.String(64), nullable=False, server_default="manual"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_stations_station_id", "stations", ["station_id"])

    if not _table_exists("station_observations"):
        op.create_table(
            "station_observations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("station_id", sa.String(64), nullable=False),
            sa.Column("obs_date", sa.Date(), nullable=False),
            sa.Column("precip_mm", sa.Float(), nullable=True),
            sa.Column("temp_max_c", sa.Float(), nullable=True),
            sa.Column("temp_min_c", sa.Float(), nullable=True),
            sa.Column("temp_mean_c", sa.Float(), nullable=True),
            sa.Column("humidity_pct", sa.Float(), nullable=True),
            sa.Column("wind_speed_ms", sa.Float(), nullable=True),
            sa.Column("pressure_hpa", sa.Float(), nullable=True),
            sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
            sa.Column("is_provisional", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("station_id", "obs_date", name="uq_station_obs_date"),
        )
        op.create_index("ix_station_obs_station_id", "station_observations", ["station_id"])
        op.create_index("ix_station_obs_date", "station_observations", ["obs_date"])


def downgrade() -> None:
    if _table_exists("station_observations"):
        op.drop_table("station_observations")
    if _table_exists("stations"):
        op.drop_table("stations")
