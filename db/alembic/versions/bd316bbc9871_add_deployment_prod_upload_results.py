"""add deployment_prod_upload_results

Revision ID: bd316bbc9871
Revises: 48c8132ced63
Create Date: 2026-04-26 17:00:39.420987

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "bd316bbc9871"
down_revision: Union[str, Sequence[str], None] = "48c8132ced63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployment_prod_upload_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("client_hostname", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("file_name", sa.String(length=512), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "success",
                "partial_upload",
                "download_failed",
                name="produploadstatus",
            ),
            nullable=False,
        ),
        sa.Column("address", sa.String(length=128), nullable=True),
        sa.Column("chunks_stored", sa.Integer(), nullable=False),
        sa.Column("chunks_failed", sa.Integer(), nullable=False),
        sa.Column("total_chunks", sa.Integer(), nullable=False),
        sa.Column("store_concurrency", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("upload_secs", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "source_url",
            name="uq_deployment_prod_upload_results_group",
        ),
    )
    op.create_index(
        op.f("ix_deployment_prod_upload_results_deployment_id"),
        "deployment_prod_upload_results",
        ["deployment_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_deployment_prod_upload_results_deployment_id"),
        table_name="deployment_prod_upload_results",
    )
    op.drop_table("deployment_prod_upload_results")
    sa.Enum(name="produploadstatus").drop(op.get_bind(), checkfirst=False)
