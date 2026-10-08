"""add prod_canary tables

Revision ID: 5ab98e6205e0
Revises: a73d175696b4
Create Date: 2026-05-12 00:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "5ab98e6205e0"
down_revision: Union[str, Sequence[str], None] = "a73d175696b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "prod_canary_uploads",
        sa.Column("canary_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("file_name", sa.String(length=512), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("top_address", sa.String(length=128), nullable=False),
        sa.Column("num_chunks", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "initial_verify_status",
            sa.Enum(
                "ok",
                "failed",
                name="prodcanaryinitialverifystatus",
            ),
            nullable=False,
        ),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("canary_id"),
    )
    op.create_index(
        op.f("ix_prod_canary_uploads_deployment_id"),
        "prod_canary_uploads",
        ["deployment_id"],
        unique=False,
    )

    op.create_table(
        "prod_canary_chunks",
        sa.Column("canary_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_address", sa.String(length=128), nullable=False),
        sa.Column(
            "chunk_role",
            sa.Enum(
                "data_map",
                "data",
                name="prodcanarychunkrole",
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["canary_id"], ["prod_canary_uploads.canary_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("canary_id", "chunk_address"),
    )

    op.create_table(
        "prod_canary_probes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("canary_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("probed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("probed_by_host", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "ok",
                "failed",
                "inconclusive",
                name="prodcanaryprobestatus",
            ),
            nullable=False,
        ),
        sa.Column("missing_chunk_address", sa.String(length=128), nullable=True),
        sa.Column(
            "terminal_peer_outcomes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["canary_id"], ["prod_canary_uploads.canary_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "canary_id",
            "probed_at",
            "probed_by_host",
            name="uq_prod_canary_probes_group",
        ),
    )
    op.create_index(
        op.f("ix_prod_canary_probes_canary_id"),
        "prod_canary_probes",
        ["canary_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_prod_canary_probes_probed_at"),
        "prod_canary_probes",
        ["probed_at"],
        unique=False,
    )

    op.create_table(
        "prod_canary_loss",
        sa.Column("canary_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("first_failed_probe_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_lost_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("missing_chunk_address", sa.String(length=128), nullable=False),
        sa.Column("hours_to_first_failure", sa.Float(), nullable=False),
        sa.Column("hours_to_confirmed_loss", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ["canary_id"], ["prod_canary_uploads.canary_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("canary_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("prod_canary_loss")
    op.drop_index(
        op.f("ix_prod_canary_probes_probed_at"),
        table_name="prod_canary_probes",
    )
    op.drop_index(
        op.f("ix_prod_canary_probes_canary_id"),
        table_name="prod_canary_probes",
    )
    op.drop_table("prod_canary_probes")
    op.drop_table("prod_canary_chunks")
    op.drop_index(
        op.f("ix_prod_canary_uploads_deployment_id"),
        table_name="prod_canary_uploads",
    )
    op.drop_table("prod_canary_uploads")
    sa.Enum(name="prodcanaryprobestatus").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="prodcanarychunkrole").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="prodcanaryinitialverifystatus").drop(op.get_bind(), checkfirst=False)
