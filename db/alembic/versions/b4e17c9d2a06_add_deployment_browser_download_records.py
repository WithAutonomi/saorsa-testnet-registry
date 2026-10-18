"""add deployment_browser_download_records

The `browser_downloaders` client tier (V2-1617) writes one `browser_download`
Influx point per Chromium instance per round. InfluxDB retention is bounded and
the browser VMs are destroyed after the rounds, while the registry's Postgres is
where every other per-operation record lives -- so without this table the only
instrument that can measure hot-record read spreading (ADR-0023) leaves nothing
behind.

Its own table rather than rows in `deployment_download_records`, for the same
reason the harness writes its own measurement: a synchronised cohort hammering
ONE file and a client drawing random files are two unlike workloads, and pooling
them would let any success rate or percentile average the two. It also carries
the round, the instance and the three per-dial summaries derived from the
browser's own trace, none of which a file transfer has.

`oprecordstatus` is REUSED (`create_type=False`), not redeclared: the three
values are exactly success / failure / skipped and they mean exactly what they
mean on a file op. The downgrade therefore does NOT drop the type -- it belongs
to `72f6ffb82c1a`, and dropping it would take the two op-record tables with it.

Revision ID: b4e17c9d2a06
Revises: a9d5f31e07c4
Create Date: 2026-10-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b4e17c9d2a06"
down_revision: Union[str, Sequence[str], None] = "a9d5f31e07c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "deployment_browser_download_records"
_STATUS_ENUM_NAME = "oprecordstatus"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("client_hostname", sa.String(length=255), nullable=False),
        sa.Column("extension", sa.String(length=64), nullable=True),
        sa.Column("region", sa.String(length=64), nullable=True),
        sa.Column("round_id", sa.BigInteger(), nullable=False),
        sa.Column("instance", sa.SmallInteger(), nullable=False),
        sa.Column("hot_file_index", sa.SmallInteger(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            # `postgresql.ENUM`, not `sa.Enum`: only the dialect type honours
            # `create_type=False`, and the generic one emits a `CREATE TYPE`
            # that fails with "type oprecordstatus already exists" against any
            # database `72f6ffb82c1a` has run on -- i.e. every real one.
            # `native_enum=False` on the other dialects, so the SQLite fixtures
            # (which have no enum types at all) still create the table.
            postgresql.ENUM(
                "success",
                "failure",
                "skipped",
                name=_STATUS_ENUM_NAME,
                create_type=False,
            ).with_variant(
                sa.Enum(
                    "success",
                    "failure",
                    "skipped",
                    name=_STATUS_ENUM_NAME,
                    native_enum=False,
                ),
                "sqlite",
            ),
            nullable=False,
        ),
        sa.Column("error_kind", sa.String(length=32), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("connect_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_late_ms", sa.Integer(), nullable=True),
        sa.Column("seeds", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("stalls", sa.Integer(), nullable=True),
        sa.Column("refused_dials", sa.Integer(), nullable=True),
        sa.Column(
            "first_reads", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("trace_s3_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "round_id",
            "instance",
            name="uq_deployment_browser_download_records_round",
        ),
    )
    op.create_index(
        op.f("ix_deployment_browser_download_records_deployment_id"),
        _TABLE,
        ["deployment_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_deployment_browser_download_records_deployment_id"),
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)
