"""add anomaly_score and is_anomaly to forecast_uploads

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-06-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'e2f3a4b5c6d7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_context().connection
    cols = {r[1] for r in conn.execute(sa.text("PRAGMA table_info(forecast_uploads)")).fetchall()}
    with op.batch_alter_table('forecast_uploads') as batch:
        if 'anomaly_score' not in cols:
            batch.add_column(sa.Column('anomaly_score', sa.Float(), nullable=True))
        if 'is_anomaly' not in cols:
            batch.add_column(sa.Column('is_anomaly', sa.Boolean(), nullable=True))


def downgrade():
    with op.batch_alter_table('forecast_uploads') as batch:
        batch.drop_column('is_anomaly')
        batch.drop_column('anomaly_score')
