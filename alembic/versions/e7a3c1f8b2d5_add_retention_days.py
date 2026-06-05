"""add retention_days to sync_config

Revision ID: e7a3c1f8b2d5
Revises: d6e4f9a2b1c8
Create Date: 2026-06-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7a3c1f8b2d5"
down_revision: Union[str, None] = "d6e4f9a2b1c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sync_config",
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="90"),
    )


def downgrade() -> None:
    op.drop_column("sync_config", "retention_days")
