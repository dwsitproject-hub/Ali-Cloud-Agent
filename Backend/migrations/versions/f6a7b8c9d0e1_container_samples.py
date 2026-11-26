"""per-container usage history, for sizing memory limits from evidence

`Instance.containers_json` holds only the latest sample, so there was no way to
ask what a service's peak memory was over a fortnight - which is exactly the
question a `mem_limit` answers. Setting one from a single observation risks the
opposite failure: a cap below the real working set turns a healthy service into
an OOM-kill loop.

One row per container per scan. At 122 containers and a 5-minute interval that
is ~35k rows/day, ~1M at the 30-day retention - small rows, pruned on the same
cadence as scan history.

`mem_bytes`/`cpu_pct` are nullable: a stopped container reports no usage, and
that is not zero usage.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
"""
import sqlalchemy as sa
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "container_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("image", sa.String(length=255)),
        sa.Column("health", sa.String(length=20)),
        sa.Column("cpu_pct", sa.Float()),          # share of the whole host
        sa.Column("mem_bytes", sa.BigInteger()),   # absolute, not MemPerc
        sa.Column("mem_limit_bytes", sa.BigInteger()),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
    )
    # The query this table exists for: one container's usage over a window.
    op.create_index("ix_container_samples_lookup", "container_samples",
                    ["instance_id", "name", "sampled_at"])
    # And the prune, which sweeps by age alone.
    op.create_index("ix_container_samples_sampled_at", "container_samples",
                    ["sampled_at"])


def downgrade():
    op.drop_index("ix_container_samples_sampled_at", table_name="container_samples")
    op.drop_index("ix_container_samples_lookup", table_name="container_samples")
    op.drop_table("container_samples")
