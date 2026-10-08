"""add deployment upload and download op records

Revision ID: 72f6ffb82c1a
Revises: c2a14b3df7e1
Create Date: 2026-05-20 15:33:22.644627

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "72f6ffb82c1a"
down_revision: Union[str, Sequence[str], None] = "c2a14b3df7e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_STATUS_ENUM_NAME = "oprecordstatus"


def _create_op_record_table(
    table_name: str, uq_name: str, ix_name: str, status_enum: sa.Enum
) -> None:
    op.create_table(
        table_name,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("client_hostname", sa.String(length=255), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", status_enum, nullable=False),
        sa.Column("start_ns", sa.BigInteger(), nullable=False),
        sa.Column("influx_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "start_ns",
            name=uq_name,
        ),
    )
    op.create_index(ix_name, table_name, ["deployment_id"], unique=False)


def upgrade() -> None:
    """Upgrade schema."""
    _create_op_record_table(
        "deployment_upload_records",
        uq_name="uq_deployment_upload_records_op",
        ix_name=op.f("ix_deployment_upload_records_deployment_id"),
        status_enum=sa.Enum(
            "success",
            "failure",
            name=_STATUS_ENUM_NAME,
        ),
    )
    _create_op_record_table(
        "deployment_download_records",
        uq_name="uq_deployment_download_records_op",
        ix_name=op.f("ix_deployment_download_records_deployment_id"),
        status_enum=sa.Enum(
            "success",
            "failure",
            name=_STATUS_ENUM_NAME,
            create_type=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_deployment_download_records_deployment_id"),
        table_name="deployment_download_records",
    )
    op.drop_table("deployment_download_records")
    op.drop_index(
        op.f("ix_deployment_upload_records_deployment_id"),
        table_name="deployment_upload_records",
    )
    op.drop_table("deployment_upload_records")
    sa.Enum(name=_STATUS_ENUM_NAME).drop(op.get_bind(), checkfirst=False)
