"""Merge independent migration branches

Revision ID: b5c6d7e8f9a0
Revises: e2f3a4b5c6d7, f8b4d2e9a3c1
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa

revision = "b5c6d7e8f9a0"
down_revision = ("e2f3a4b5c6d7", "f8b4d2e9a3c1")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
