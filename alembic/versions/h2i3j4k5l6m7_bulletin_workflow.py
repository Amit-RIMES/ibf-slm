"""bulletin workflow fields

Revision ID: h2i3j4k5l6m7
Revises: g1h2i3j4k5l6
Create Date: 2026-06-17 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "h2i3j4k5l6m7"
down_revision: Union[str, None] = "g1h2i3j4k5l6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _col_exists(table: str, col: str) -> bool:
    conn = op.get_bind()
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == col for r in rows)


def upgrade() -> None:
    with op.batch_alter_table("bulletin_drafts") as batch:
        if not _col_exists("bulletin_drafts", "submitted_by_id"):
            batch.add_column(sa.Column("submitted_by_id", sa.Integer(), nullable=True))
        if not _col_exists("bulletin_drafts", "submitted_at"):
            batch.add_column(sa.Column("submitted_at", sa.DateTime(), nullable=True))
        if not _col_exists("bulletin_drafts", "approved_by_id"):
            batch.add_column(sa.Column("approved_by_id", sa.Integer(), nullable=True))
        if not _col_exists("bulletin_drafts", "approved_at"):
            batch.add_column(sa.Column("approved_at", sa.DateTime(), nullable=True))
        if not _col_exists("bulletin_drafts", "sent_at"):
            batch.add_column(sa.Column("sent_at", sa.DateTime(), nullable=True))
        if not _col_exists("bulletin_drafts", "approval_notes"):
            batch.add_column(sa.Column("approval_notes", sa.String(512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("bulletin_drafts") as batch:
        for col in ("approval_notes", "sent_at", "approved_at", "approved_by_id",
                    "submitted_at", "submitted_by_id"):
            if _col_exists("bulletin_drafts", col):
                batch.drop_column(col)
