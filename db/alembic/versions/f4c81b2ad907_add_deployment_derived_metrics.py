"""add deployment_derived_metrics

Canonical, normalised per-run metrics — "outbound bytes per GB of user traffic"
and "disk consumed per GB stored", plus the components each is built from —
computed at extraction time and stored beside the raw data.

Both were NOT ASSESSED in the rc.1 gate verdict because only raw measurements
existed: runs of different lengths and workloads are not comparable without
normalising against the traffic actually served in the same window.

A tall keyed table rather than typed columns per metric, so the
comparison-report command can add gate metrics without a migration each time.
No backfill: for a past run the source InfluxDB is destroyed with its
monitoring stack, and occupancy was never archived at all.

Revision ID: f4c81b2ad907
Revises: a3f7c1e59b42
Create Date: 2026-07-25 09:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f4c81b2ad907"
down_revision: Union[str, Sequence[str], None] = "a3f7c1e59b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployment_derived_metrics",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("window_label", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("numerator", sa.Float(), nullable=True),
        sa.Column("denominator", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # One value per metric per window per deployment: recomputing updates in
        # place rather than leaving rival answers a comparison would pick
        # between non-deterministically.
        sa.UniqueConstraint(
            "deployment_id",
            "metric",
            "window_label",
            name="uq_deployment_derived_metrics_window",
        ),
    )
    op.create_index(
        op.f("ix_deployment_derived_metrics_deployment_id"),
        "deployment_derived_metrics",
        ["deployment_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_deployment_derived_metrics_deployment_id"),
        table_name="deployment_derived_metrics",
    )
    op.drop_table("deployment_derived_metrics")
