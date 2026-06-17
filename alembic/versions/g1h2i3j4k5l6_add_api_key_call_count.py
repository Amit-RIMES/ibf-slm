"""add api_key call_count

Revision ID: g1h2i3j4k5l6
Revises: f8b4d2e9a3c1
Create Date: 2026-06-17 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "g1h2i3j4k5l6"
down_revision: Union[str, None] = "e0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("call_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("api_keys", "call_count")
