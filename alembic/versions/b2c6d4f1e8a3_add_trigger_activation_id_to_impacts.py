"""add trigger_activation_id to impacts

Revision ID: b2c6d4f1e8a3
Revises: a9b5c3d2e1f4
Create Date: 2026-06-08

"""
from typing import Union
import sqlalchemy as sa
from alembic import op

revision: str = "b2c6d4f1e8a3"
down_revision: Union[str, None] = "a9b5c3d2e1f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_context().connection
    existing_cols = [row[1] for row in conn.execute(sa.text("PRAGMA table_info(impact_records)"))]
    if "trigger_activation_id" not in existing_cols:
        op.add_column(
            "impact_records",
            sa.Column("trigger_activation_id", sa.Integer(), nullable=True),
        )
    existing_indexes = [
        row[0] for row in conn.execute(sa.text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='impact_records'"
        ))
    ]
    if "ix_impact_records_trigger_activation_id" not in existing_indexes:
        op.create_index("ix_impact_records_trigger_activation_id", "impact_records", ["trigger_activation_id"])


def downgrade() -> None:
    op.drop_index("ix_impact_records_trigger_activation_id", "impact_records")
    op.drop_column("impact_records", "trigger_activation_id")
