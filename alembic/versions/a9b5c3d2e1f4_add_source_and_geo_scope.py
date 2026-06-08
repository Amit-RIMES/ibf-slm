"""add source and geo scope

Revision ID: a9b5c3d2e1f4
Revises: f8b4d2e9a3c1
Create Date: 2026-06-08

"""
from typing import Union
import sqlalchemy as sa
from alembic import op

revision: str = "a9b5c3d2e1f4"
down_revision: Union[str, None] = "f8b4d2e9a3c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("forecast_uploads", sa.Column("source", sa.String(64), nullable=True))
    op.add_column("triggers", sa.Column("scope_lat_min", sa.Float(), nullable=True))
    op.add_column("triggers", sa.Column("scope_lat_max", sa.Float(), nullable=True))
    op.add_column("triggers", sa.Column("scope_lon_min", sa.Float(), nullable=True))
    op.add_column("triggers", sa.Column("scope_lon_max", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("forecast_uploads", "source")
    op.drop_column("triggers", "scope_lat_min")
    op.drop_column("triggers", "scope_lat_max")
    op.drop_column("triggers", "scope_lon_min")
    op.drop_column("triggers", "scope_lon_max")
