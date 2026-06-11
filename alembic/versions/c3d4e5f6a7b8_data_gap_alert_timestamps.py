"""add data gap alert timestamps to sync_config

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-11
"""
import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(sync_config)")).fetchall()}
    with op.batch_alter_table("sync_config") as batch_op:
        if "last_chirps_gap_alert_at" not in cols:
            batch_op.add_column(
                sa.Column("last_chirps_gap_alert_at", sa.DateTime(timezone=True), nullable=True)
            )
        if "last_forecast_gap_alert_at" not in cols:
            batch_op.add_column(
                sa.Column("last_forecast_gap_alert_at", sa.DateTime(timezone=True), nullable=True)
            )


def downgrade() -> None:
    with op.batch_alter_table("sync_config") as batch_op:
        batch_op.drop_column("last_forecast_gap_alert_at")
        batch_op.drop_column("last_chirps_gap_alert_at")
