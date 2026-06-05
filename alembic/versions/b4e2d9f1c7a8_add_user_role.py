"""add user role

Revision ID: b4e2d9f1c7a8
Revises: a3f1c2d4e5b6
Create Date: 2026-06-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4e2d9f1c7a8"
down_revision: Union[str, None] = "a3f1c2d4e5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("role", sa.String(50), nullable=False, server_default="user"),
    )
    # Promote all existing users to admin so no one gets locked out
    op.execute("UPDATE users SET role = 'admin'")


def downgrade() -> None:
    op.drop_column("users", "role")
