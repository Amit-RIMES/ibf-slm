"""SMS / WhatsApp channel

Revision ID: i3j4k5l6m7n8
Revises: h2i3j4k5l6m7
Create Date: 2026-06-17 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i3j4k5l6m7n8"
down_revision: Union[str, None] = "h2i3j4k5l6m7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _col_exists(table: str, col: str) -> bool:
    conn = op.get_bind()
    rows = conn.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == col for r in rows)


def _table_exists(table: str) -> bool:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    ).fetchall()
    return len(rows) > 0


def upgrade() -> None:
    # Add phone fields to alert_recipients
    with op.batch_alter_table("alert_recipients") as batch:
        if not _col_exists("alert_recipients", "phone"):
            batch.add_column(sa.Column("phone", sa.String(32), nullable=True))
        if not _col_exists("alert_recipients", "whatsapp_enabled"):
            batch.add_column(sa.Column("whatsapp_enabled", sa.Boolean(), nullable=False, server_default="0"))

    # Create sms_config singleton table
    if not _table_exists("sms_config"):
        op.create_table(
            "sms_config",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider", sa.String(32), nullable=False, server_default="twilio"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("account_sid", sa.String(256), nullable=True),
            sa.Column("auth_token", sa.String(256), nullable=True),
            sa.Column("from_number", sa.String(32), nullable=True),
            sa.Column("whatsapp_enabled", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("whatsapp_from", sa.String(64), nullable=True),
            sa.Column("webhook_url", sa.String(512), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("alert_recipients") as batch:
        for col in ("whatsapp_enabled", "phone"):
            if _col_exists("alert_recipients", col):
                batch.drop_column(col)

    if _table_exists("sms_config"):
        op.drop_table("sms_config")
