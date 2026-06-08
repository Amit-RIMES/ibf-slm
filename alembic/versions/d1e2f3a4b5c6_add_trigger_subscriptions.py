"""add trigger_subscriptions table

Revision ID: d1e2f3a4b5c6
Revises: c3d7e5f2a1b9
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'd1e2f3a4b5c6'
down_revision = 'c3d7e5f2a1b9'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_context().connection
    tables = {r[0] for r in conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    if 'trigger_subscriptions' not in tables:
        op.create_table(
            'trigger_subscriptions',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('trigger_id', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        with op.batch_alter_table('trigger_subscriptions') as batch:
            batch.create_index('ix_trigger_subscriptions_user_id', ['user_id'])
            batch.create_index('ix_trigger_subscriptions_trigger_id', ['trigger_id'])


def downgrade():
    op.drop_table('trigger_subscriptions')
