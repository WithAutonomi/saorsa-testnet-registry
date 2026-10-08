"""add prod_canary_unavailability table

Revision ID: add_canary_unavail
Revises: b7e1c9a4d2f8
Create Date: 2026-06-02 23:58:09.204113

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "add_canary_unavail"
down_revision: Union[str, Sequence[str], None] = "b7e1c9a4d2f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    One row per *unavailability episode* of a canary: a maximal run during
    which the chunk was confirmed unreachable (two-strike) and then either
    recovered or is still ongoing. Derived from the probe stream by
    record-results; replaces the binary "lost" view with measured windows.
    """
    op.create_table(
        "prod_canary_unavailability",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "canary_id",
            sa.Uuid(),
            sa.ForeignKey("prod_canary_uploads.canary_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("episode_no", sa.Integer(), nullable=False),
        sa.Column("missing_chunk_address", sa.String(128), nullable=False),
        sa.Column("first_failed_probe_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "confirmed_unavailable_at", sa.DateTime(timezone=True), nullable=False
        ),
        # NULL while the episode is still ongoing (chunk not yet recovered).
        sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hours_to_confirmed", sa.Float(), nullable=False),
        # NULL while ongoing; hours from confirmed_unavailable_at to recovery.
        sa.Column("unavailable_hours", sa.Float(), nullable=True),
        sa.Column("recovered", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "canary_id", "episode_no", name="uq_canary_unavail_episode"
        ),
    )
    op.create_index(
        "ix_prod_canary_unavailability_canary_id",
        "prod_canary_unavailability",
        ["canary_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_prod_canary_unavailability_canary_id",
        table_name="prod_canary_unavailability",
    )
    op.drop_table("prod_canary_unavailability")
