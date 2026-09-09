"""instance specs + docker container inventory

Adds provider-reported hardware specs (vCPU/RAM/disk/type/status) and the
host-reported docker container list to `instances`. All nullable: an instance the
provider cannot describe, or a host with no SSH access, still renders.

Revision ID: d3e4f5a6b7c8
Revises: c7d8e9f0a1b2
"""
import sqlalchemy as sa
from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("instance_type", sa.String(length=60)),
    ("vcpu", sa.Integer()),
    ("memory_mb", sa.Integer()),
    ("disk_gb", sa.Integer()),
    ("platform", sa.String(length=120)),
    ("provider_status", sa.String(length=40)),
    ("specs_updated_at", sa.DateTime(timezone=True)),
    ("containers_json", sa.JSON()),
    ("containers_error", sa.Text()),
    ("containers_updated_at", sa.DateTime(timezone=True)),
)


def upgrade():
    with op.batch_alter_table("instances") as batch:
        for name, type_ in _COLUMNS:
            batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade():
    with op.batch_alter_table("instances") as batch:
        for name, _ in reversed(_COLUMNS):
            batch.drop_column(name)
