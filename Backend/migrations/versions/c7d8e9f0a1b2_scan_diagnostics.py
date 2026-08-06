"""store on-breach diagnostics with the scan run

Revision ID: c7d8e9f0a1b2
Revises: b1c2d3e4f5a6
Create Date: 2026-08-05 05:10:00.000000

Diagnostics collected over SSH when a breach occurs were previously rendered into
the alert email and then discarded. Persisting them lets the dashboard show the
same evidence (which container/query caused the breach) instead of email-only.
"""
from alembic import op
import sqlalchemy as sa


revision = 'c7d8e9f0a1b2'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('scan_runs', sa.Column('diagnostics', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('scan_runs', 'diagnostics')
