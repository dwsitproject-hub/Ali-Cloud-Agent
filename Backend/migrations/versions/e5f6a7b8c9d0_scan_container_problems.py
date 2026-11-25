"""per-scan record of containers docker calls broken

`Instance.containers_json` holds only the CURRENT container list, so there was no
way to ask "was this container unhealthy two scans ago". alerting.decide() needs
exactly that: it confirms a problem across ALERT_CONFIRM_SCANS consecutive scans
before emailing, and declares recovery the same way.

Without it, an unhealthy container was shown on the dashboard, ranked in the
triage queue and given a runbook, while being invisible to the code that decides
whether to email - so `pct-api` served nothing on Backend Production for 2h20m and
the system sent "All systems healthy" over the top of it.

Nullable: a scan written before this column existed, or by a build without
container collection, simply has none.

Revision ID: e5f6a7b8c9d0
Revises: d3e4f5a6b7c8
"""
import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("scan_runs") as batch:
        batch.add_column(sa.Column("container_problems", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("scan_runs") as batch:
        batch.drop_column("container_problems")
