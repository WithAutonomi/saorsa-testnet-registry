"""add deployment_host_metric_buckets

Bucketed host metrics, so an arbitrary sub-window of a run can be re-derived
from Postgres after the monitoring stack (and its InfluxDB) is destroyed.

Written alongside the existing `deployment_host_results` rollup row, which is
unchanged and remains the cheap whole-run view. No backfill is possible for
deployments recorded before this table existed.

Revision ID: a3f7c1e59b42
Revises: add_prod_upload_cost
Create Date: 2026-07-25 05:50:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a3f7c1e59b42"
down_revision: Union[str, Sequence[str], None] = "add_prod_upload_cost"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployment_host_metric_buckets",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("device", sa.String(length=64), nullable=True),
        sa.Column("volume_role", sa.String(length=32), nullable=True),
        sa.Column("service", sa.String(length=128), nullable=True),
        sa.Column("avg", sa.Float(), nullable=True),
        sa.Column("min", sa.Float(), nullable=True),
        sa.Column("max", sa.Float(), nullable=True),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # Deliberately the ONLY index: every read filters on a deployment and a
    # `bucket_start` range, then groups by host/metric. A second index on
    # (deployment_id, host, metric) would do no work for that query while
    # costing insert time and disk on every row.
    op.create_index(
        "ix_deployment_host_metric_buckets_window",
        "deployment_host_metric_buckets",
        ["deployment_id", "bucket_start"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_deployment_host_metric_buckets_window",
        table_name="deployment_host_metric_buckets",
    )
    op.drop_table("deployment_host_metric_buckets")
