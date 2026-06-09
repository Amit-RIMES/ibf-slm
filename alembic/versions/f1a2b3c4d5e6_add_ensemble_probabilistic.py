"""Add ensemble/probabilistic fields to forecasts and triggers.

Revision ID: f1a2b3c4d5e6
Revises: e3f4a5b6c7d8
Create Date: 2026-06-09

"""
from alembic import op
import sqlalchemy as sa

revision = "f1a2b3c4d5e6"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def _col_exists(table: str, column: str) -> bool:
    conn = op.get_bind()
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def upgrade() -> None:
    # forecast_uploads: ensemble columns
    for col, typedef in [
        ("ensemble_size",  "INTEGER"),
        ("precip_p10",     "FLOAT"),
        ("precip_p25",     "FLOAT"),
        ("precip_p50",     "FLOAT"),
        ("precip_p75",     "FLOAT"),
        ("precip_p90",     "FLOAT"),
        ("exceedance_json","TEXT"),
    ]:
        if not _col_exists("forecast_uploads", col):
            op.execute(sa.text(f"ALTER TABLE forecast_uploads ADD COLUMN {col} {typedef}"))

    # triggers: probability_threshold
    if not _col_exists("triggers", "probability_threshold"):
        op.execute(sa.text("ALTER TABLE triggers ADD COLUMN probability_threshold FLOAT"))

    # trigger_activations: probability
    if not _col_exists("trigger_activations", "probability"):
        op.execute(sa.text("ALTER TABLE trigger_activations ADD COLUMN probability FLOAT"))


def downgrade() -> None:
    # SQLite does not support DROP COLUMN before 3.35; skip downgrade.
    pass
