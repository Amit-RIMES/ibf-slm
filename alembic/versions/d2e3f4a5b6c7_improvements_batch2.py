"""Batch-2 improvements: response plan, lead-time stats, seasonal anomaly, IP allowlist, TOTP, activation comments

Revision ID: d2e3f4a5b6c7
Revises: c8d9e0f1a2b3
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa

revision = "d2e3f4a5b6c7"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # triggers: response_plan
    t_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(triggers)")).fetchall()}
    with op.batch_alter_table("triggers") as b:
        if "response_plan" not in t_cols:
            b.add_column(sa.Column("response_plan", sa.Text(), nullable=True))

    # forecast_uploads: lead_time_stats, seasonal_anomaly_pct
    f_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(forecast_uploads)")).fetchall()}
    with op.batch_alter_table("forecast_uploads") as b:
        if "lead_time_stats" not in f_cols:
            b.add_column(sa.Column("lead_time_stats", sa.Text(), nullable=True))
        if "seasonal_anomaly_pct" not in f_cols:
            b.add_column(sa.Column("seasonal_anomaly_pct", sa.Float(), nullable=True))

    # api_keys: allowed_ips
    k_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(api_keys)")).fetchall()}
    with op.batch_alter_table("api_keys") as b:
        if "allowed_ips" not in k_cols:
            b.add_column(sa.Column("allowed_ips", sa.String(1024), nullable=True))

    # users: totp_secret, totp_enabled
    u_cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(users)")).fetchall()}
    with op.batch_alter_table("users") as b:
        if "totp_secret" not in u_cols:
            b.add_column(sa.Column("totp_secret", sa.String(64), nullable=True))
        if "totp_enabled" not in u_cols:
            b.add_column(sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default="0"))

    # activation_comments table
    tables = {r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    if "activation_comments" not in tables:
        op.create_table(
            "activation_comments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("activation_id", sa.Integer(),
                      sa.ForeignKey("trigger_activations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_activation_comments_activation_id", "activation_comments", ["activation_id"])


def downgrade() -> None:
    op.drop_table("activation_comments")
    with op.batch_alter_table("users") as b:
        b.drop_column("totp_enabled")
        b.drop_column("totp_secret")
    with op.batch_alter_table("api_keys") as b:
        b.drop_column("allowed_ips")
    with op.batch_alter_table("forecast_uploads") as b:
        b.drop_column("seasonal_anomaly_pct")
        b.drop_column("lead_time_stats")
    with op.batch_alter_table("triggers") as b:
        b.drop_column("response_plan")
