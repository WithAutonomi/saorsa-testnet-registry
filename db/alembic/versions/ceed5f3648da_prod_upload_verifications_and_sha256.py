"""prod upload verifications, upload sha256, continuous-uploader claim source

Supports the daily production write test (V2-837):

* ``deployment_prod_upload_verifications`` — one row per (deployment,
  verifying host, address). Also serves as the verifier VMs' work queue, which
  is why they keep no local state.
* ``deployment_prod_upload_results.sha256`` — the hash the uploader computes
  over the exact bytes it uploaded, which is what a verifier compares against.
* ``claimsource`` gains ``prod-continuous-uploader``.

Revision ID: ceed5f3648da
Revises: f5b31a7c9e04
Create Date: 2026-08-13 23:37:11.096082

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "ceed5f3648da"
down_revision: Union[str, Sequence[str], None] = "f5b31a7c9e04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployment_prod_upload_verifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("client_hostname", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("address", sa.String(length=128), nullable=False),
        sa.Column("file_name", sa.String(length=512), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "ok",
                "digest_mismatch",
                "download_failed",
                "inconclusive",
                name="produploadverifystatus",
            ),
            nullable=False,
        ),
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
        sa.Column("actual_sha256", sa.String(length=64), nullable=True),
        sa.Column("bytes_downloaded", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("age_at_check_secs", sa.BigInteger(), nullable=True),
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
            "address",
            name="uq_deployment_prod_upload_verifications_group",
        ),
    )
    op.create_index(
        op.f("ix_deployment_prod_upload_verifications_deployment_id"),
        "deployment_prod_upload_verifications",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        "ix_prod_upload_verifications_queue",
        "deployment_prod_upload_verifications",
        ["deployment_id", "client_hostname"],
        unique=False,
    )
    op.add_column(
        "deployment_prod_upload_results",
        sa.Column("sha256", sa.String(length=64), nullable=True),
    )

    # Postgres refuses ALTER TYPE ... ADD VALUE inside a transaction that later
    # uses the new label, so it goes in its own autocommit block. IF NOT EXISTS
    # keeps the migration re-runnable against a database where it was added by
    # hand.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE claimsource ADD VALUE IF NOT EXISTS 'prod-continuous-uploader'"
        )


def downgrade() -> None:
    """Downgrade schema.

    The `claimsource` label is deliberately NOT removed. Postgres has no DROP
    VALUE, so undoing it means rewriting the type and every column that uses
    it — and any claim already recorded under the new source would have to be
    destroyed or relabelled to something untrue. An unused extra label is
    harmless; losing the record of what took a file is not.
    """
    op.drop_column("deployment_prod_upload_results", "sha256")
    op.drop_index(
        "ix_prod_upload_verifications_queue",
        table_name="deployment_prod_upload_verifications",
    )
    op.drop_index(
        op.f("ix_deployment_prod_upload_verifications_deployment_id"),
        table_name="deployment_prod_upload_verifications",
    )
    op.drop_table("deployment_prod_upload_verifications")
    sa.Enum(name="produploadverifystatus").drop(op.get_bind(), checkfirst=True)
