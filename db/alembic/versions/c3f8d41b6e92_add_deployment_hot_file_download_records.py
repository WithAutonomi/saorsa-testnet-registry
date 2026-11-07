"""add deployment_hot_file_download_records

The `hot_file_readers` client tier (INFRA-531) writes one `hot_file_download`
Influx point per native `ant file download` reader per round. InfluxDB retention
is bounded and the reader VMs are destroyed after the rounds, while the
registry's Postgres is where every other per-operation record lives -- so
without this table the only instrument that can measure the ADR-0026 peer swarm
leaves nothing behind.

Its own table rather than rows in `deployment_download_records`, for the same
reason the reader writes its own measurement: a synchronised cohort hammering
ONE file and a client drawing random files are two unlike workloads, and pooling
them would let any success rate or percentile average the two -- and would put
this cohort's failures into the native hard floors `deployments compare` gates
on. It also carries the round, the reader inside the VM, and the twenty-five
swarm counters the CLI prints, none of which a file transfer has.

`swarm_present` is what makes a zero readable: a released `ant` (0.3.13) prints
the result object WITHOUT `swarm`, so the control cohort of a branch-vs-release
comparison carries zeros throughout, and without the flag those would be
indistinguishable from a swarm that formed and shared nothing.

The unique key carries `started_at` as well as the round and the reader,
because a `round_id` means a different instant under a different period: a
cohort recreated under the same name with 86401 s instead of 86400 s gives round
20833 two real reads, and the four-column key let the later destroy the earlier.

`oprecordstatus` is REUSED (`create_type=False`), not redeclared: the three
values are exactly success / failure / skipped and they mean exactly what they
mean on a file op. The downgrade therefore does NOT drop the type -- it belongs
to `72f6ffb82c1a`, and dropping it would take the two op-record tables with it.

Revision ID: c3f8d41b6e92
Revises: a2e71c4b9f36
Create Date: 2026-11-06

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3f8d41b6e92"
down_revision: Union[str, Sequence[str], None] = "a2e71c4b9f36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "deployment_hot_file_download_records"
_STATUS_ENUM_NAME = "oprecordstatus"

#: The swarm counters, in the order `saorsa_db.models.hot_file_download_record`
#: declares them. All BIGINT and all nullable: NULL means "not recorded", which
#: a byte counter's 0 does not.
_SWARM_COUNTERS = (
    "records_from_peers",
    "bytes_from_peers",
    "records_rejected",
    "asks_busy",
    "asks_missing",
    "asks_failed",
    "records_served",
    "bytes_served",
    "links",
    "rooms",
    "discovery_lookups",
    "discovery_announces",
    "discovery_announces_failed",
    "discovery_announces_timed_out",
    "discovery_announces_refused",
    "discovery_bytes_sent",
    "discovery_bytes_received",
    "discovery_offers_made",
    "discovery_offers_delivered",
    "discovery_offers_received",
    "discovery_answers_sent",
    "discovery_answers_received",
    "discovery_channels_opened",
    "discovery_links_proven",
    "discovery_links_failed",
)


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
        sa.Column("client_elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_late_ms", sa.Integer(), nullable=True),
        sa.Column("verify_status", sa.SmallInteger(), nullable=True),
        sa.Column("swarm_present", sa.Boolean(), nullable=True),
        *(sa.Column(name, sa.BigInteger(), nullable=True) for name in _SWARM_COUNTERS),
        sa.Column(
            "swarm_rooms",
            # The same `with_variant` the model declares, so the migration can
            # be applied to a fresh SQLite too: a bare `JSONB` has no SQLite
            # rendering at all and `CREATE TABLE` fails to compile.
            postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
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
        # `started_at` is part of the identity, not merely stored: a `round_id`
        # is `floor(epoch / period)`, so the same number names a different
        # instant under a different period, and two schedules' reads of one
        # round are two reads. Without it the later silently destroyed the
        # earlier.
        sa.UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "round_id",
            "instance",
            "started_at",
            name="uq_deployment_hot_file_download_records_round",
        ),
    )
    op.create_index(
        op.f("ix_deployment_hot_file_download_records_deployment_id"),
        _TABLE,
        ["deployment_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_deployment_hot_file_download_records_deployment_id"),
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)
