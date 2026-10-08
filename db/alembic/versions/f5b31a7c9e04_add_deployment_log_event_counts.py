"""add deployment_log_event_counts

Connection-error counts become a standard part of every staging run (V2-842):
a named set of log strings is counted in the deployment's Elasticsearch and the
COUNTS are recorded here, so `deployments compare` can gate on them.

The counts must be extracted while the run's Elasticsearch is still alive — it
is per-run scratch and is destroyed at teardown, so nothing can be backfilled
for a past run. Runs recorded before this table exists simply have no rows, and
the gates read NOT ASSESSED against them rather than passing.

Its own table rather than a `deployment_derived_metrics` row on purpose:
`deployments compare` refuses when the two runs' derived-metric `source` sets
differ, so adding an `elasticsearch`-sourced row to the candidate would make it
uncomparable against every existing baseline.

Revision ID: f5b31a7c9e04
Revises: c3e691d47b82
Create Date: 2026-08-11 20:40:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f5b31a7c9e04"
down_revision: Union[str, Sequence[str], None] = "c3e691d47b82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployment_log_event_counts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("event_group", sa.String(length=64), nullable=False),
        sa.Column("pattern_set", sa.String(length=64), nullable=False),
        sa.Column("pattern_version", sa.Integer(), nullable=False),
        sa.Column("pattern", sa.String(length=512), nullable=False),
        sa.Column("window_label", sa.String(length=64), nullable=False),
        # BigInteger: a staging fleet at DEBUG has produced hundreds of millions
        # of lines, and one string reaching 2^31 is not far-fetched.
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.Column("hosts", sa.Integer(), nullable=True),
        sa.Column("total_documents", sa.BigInteger(), nullable=True),
        # Nullable: a pattern with zero matches has no span, and a zero count is
        # a real measurement that must still be storable.
        sa.Column("observed_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        # One count per string per window per deployment: re-recording replaces
        # in place rather than leaving rival answers a comparison would pick
        # between non-deterministically.
        sa.UniqueConstraint(
            "deployment_id",
            "event_key",
            "window_label",
            name="uq_deployment_log_event_counts_window",
        ),
    )
    op.create_index(
        op.f("ix_deployment_log_event_counts_deployment_id"),
        "deployment_log_event_counts",
        ["deployment_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_deployment_log_event_counts_deployment_id"),
        table_name="deployment_log_event_counts",
    )
    op.drop_table("deployment_log_event_counts")
