"""add p50/p95 to deployment_host_metric_buckets

The staging release gates name node CPU *median* and node RSS *p50*, but the
bucket rows only ever held avg/min/max/samples — and a median cannot be derived
from a mean and a range at any bucket width. The comparison command therefore
had to substitute a sample-weighted mean and say so in the report.

These columns capture the percentile within each bucket, where it is exact,
computed at extraction time while the raw Influx samples are still in hand.

Nullable and NOT backfilled: for any past run the source InfluxDB has been
destroyed with its monitoring stack, so those rows keep a NULL and consumers
fall back to the mean. That is also why the columns exist rather than the
percentile being recomputed on demand — there is nothing left to recompute from.

NB percentiles do not compose across buckets the way avg/min/max do. A window
spanning several buckets can only estimate its percentile from theirs, and
`get_host_metric_window` flags its result as approximate for that reason.

Revision ID: d9f3a71c50e2
Revises: b7e4d9a2c318
Create Date: 2026-07-29 14:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9f3a71c50e2'
down_revision: Union[str, Sequence[str], None] = 'b7e4d9a2c318'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'deployment_host_metric_buckets',
        sa.Column('p50', sa.Float(), nullable=True),
    )
    op.add_column(
        'deployment_host_metric_buckets',
        sa.Column('p95', sa.Float(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('deployment_host_metric_buckets', 'p95')
    op.drop_column('deployment_host_metric_buckets', 'p50')
