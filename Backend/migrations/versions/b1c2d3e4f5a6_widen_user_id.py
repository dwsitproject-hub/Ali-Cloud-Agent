"""widen users.id for OIDC sub

Revision ID: b1c2d3e4f5a6
Revises: 483200f012b7
Create Date: 2026-07-17 00:00:00.000000

The Hub SSO moved from an HS256 bridge (UUID user_id) to OIDC; the user PK is now
the OIDC `sub` claim, which can exceed 36 chars. Widen users.id to String(255).
"""
from alembic import op
import sqlalchemy as sa


revision = 'b1c2d3e4f5a6'
down_revision = '483200f012b7'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('users', 'id',
                    existing_type=sa.String(length=36),
                    type_=sa.String(length=255),
                    existing_nullable=False)


def downgrade():
    op.alter_column('users', 'id',
                    existing_type=sa.String(length=255),
                    type_=sa.String(length=36),
                    existing_nullable=False)
