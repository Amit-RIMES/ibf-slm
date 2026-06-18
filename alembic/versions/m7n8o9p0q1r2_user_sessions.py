"""Add user_sessions table and sessions_invalidated_before on users

Revision ID: m7n8o9p0q1r2
Revises: l6m7n8o9p0q1
Create Date: 2026-06-18 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "m7n8o9p0q1r2"
down_revision: Union[str, None] = "l6m7n8o9p0q1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    ).fetchall()
    return len(rows) > 0


def _column_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def upgrade() -> None:
    if not _table_exists("user_sessions"):
        op.create_table(
            "user_sessions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ip_address", sa.String(64), nullable=True),
            sa.Column("user_agent", sa.String(512), nullable=True),
        )
    if not _column_exists("users", "sessions_invalidated_before"):
        op.add_column("users", sa.Column(
            "sessions_invalidated_before",
            sa.DateTime(timezone=True),
            nullable=True,
        ))


def downgrade() -> None:
    pass
