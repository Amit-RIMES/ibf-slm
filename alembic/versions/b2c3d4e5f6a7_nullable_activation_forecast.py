"""make trigger_activations.forecast_id nullable for SPI triggers

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-11
"""
import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite requires batch mode to change column nullability
    with op.batch_alter_table("trigger_activations") as batch_op:
        batch_op.alter_column(
            "forecast_id",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("trigger_activations") as batch_op:
        batch_op.alter_column(
            "forecast_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
