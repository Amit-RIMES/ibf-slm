"""add webhooks

Revision ID: c3d7e5f2a1b9
Revises: b2c6d4f1e8a3
Create Date: 2026-06-08

"""
from typing import Union
import sqlalchemy as sa
from alembic import op

revision: str = "c3d7e5f2a1b9"
down_revision: Union[str, None] = "b2c6d4f1e8a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhooks",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("webhooks")
