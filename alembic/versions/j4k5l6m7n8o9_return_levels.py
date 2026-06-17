"""Return level cache table

Revision ID: j4k5l6m7n8o9
Revises: i3j4k5l6m7n8
Create Date: 2026-06-17 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "j4k5l6m7n8o9"
down_revision: Union[str, None] = "i3j4k5l6m7n8"
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
    if not _table_exists("return_levels"):
        op.create_table(
            "return_levels",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("variable", sa.String(32), nullable=False, unique=True),
            sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("n_years", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("n_obs", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rl_2", sa.Float(), nullable=True),
            sa.Column("rl_5", sa.Float(), nullable=True),
            sa.Column("rl_10", sa.Float(), nullable=True),
            sa.Column("rl_25", sa.Float(), nullable=True),
            sa.Column("rl_50", sa.Float(), nullable=True),
            sa.Column("rl_100", sa.Float(), nullable=True),
            sa.Column("gev_shape", sa.Float(), nullable=True),
            sa.Column("gev_loc", sa.Float(), nullable=True),
            sa.Column("gev_scale", sa.Float(), nullable=True),
        )


def downgrade() -> None:
    if _table_exists("return_levels"):
        op.drop_table("return_levels")
