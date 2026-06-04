"""add sync_config and sync_log

Revision ID: a3f1c2d4e5b6
Revises: 9ef52e9f49bd
Create Date: 2026-06-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f1c2d4e5b6'
down_revision: Union[str, None] = '9ef52e9f49bd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sync_config',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('sources', sa.Text(), nullable=False),
        sa.Column('sync_hour', sa.Integer(), nullable=False),
        sa.Column('sync_minute', sa.Integer(), nullable=False),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_run_status', sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'sync_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=False),
        sa.Column('date', sa.String(length=8), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('forecast_id', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_sync_log_id'), 'sync_log', ['id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_sync_log_id'), table_name='sync_log')
    op.drop_table('sync_log')
    op.drop_table('sync_config')
