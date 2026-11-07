"""add the swarm and schedule columns to deployment_browser_download_records

The browser cohort (INFRA-532) gains the same three scheduling knobs the native
hot-file readers have -- a round parity, a start stagger and a seed window -- and
the branch bundle can tell it what the swarm did: `swarmStats()` /
`swarmRoomStats()` on the client, and the source of every chunk (a node, the
local cache, or another reader) in `downloadPublicFile`'s progress messages.
None of it reached the row, so a hot-read cohort's rounds recorded that a
download happened and nothing about whether the swarm served it.

The twenty-five counters are deliberately the SAME names
`deployment_hot_file_download_records` carries: one run puts a browser cohort and
a native cohort on the same file, and a figure named two ways is a figure no
comparison can make.

`swarm_present` is what makes a zero readable, exactly as on the native table: a
pinned older bundle has no `swarmStats()` at all, so its rows carry zeros
throughout, and without the flag those are indistinguishable from a swarm that
formed and shared nothing.

`start_offset_ms` is what keeps the BOUNDARY recoverable now that `started_at`
is the instance's SCHEDULED start (`round_id x period + offset`): the boundary is
`started_at - start_offset_ms`, and every row written before this migration has
no stagger, so the two are the same instant and reading the column as 0 is
correct for all of them.

Every column is nullable with no server default, as every additive column on
these tables is: NULL means NOT RECORDED, and nothing is backfilled. A row
written before this migration carries no swarm figures because none were ever
collected, and inventing zeros for it would be inventing a measurement.

Revision ID: d1a9c37f6b48
Revises: c3f8d41b6e92
Create Date: 2026-11-07

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d1a9c37f6b48"
down_revision: Union[str, Sequence[str], None] = "c3f8d41b6e92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "deployment_browser_download_records"

#: Milliseconds, INTEGER as every other `_ms` column on this table is.
_MS_COLUMNS = ("start_offset_ms", "seeded_ms")

#: Where each chunk came from, parsed out of the bundle's progress messages.
#: BIGINT because they are counts over a file of unbounded chunk count, and
#: because the counters below are.
_CHUNK_SOURCE_COUNTERS = (
    "chunks_total",
    "chunks_from_nodes",
    "chunks_from_cache",
    "chunks_from_readers",
)

#: The swarm counters, in the order
#: `saorsa_db.models.hot_file_download_record` declares them -- which is where
#: the model imports them from, so the two tables cannot drift. All BIGINT and
#: all nullable: NULL means "not recorded", which a byte counter's 0 does not.
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
    for name in _MS_COLUMNS:
        op.add_column(_TABLE, sa.Column(name, sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("swarm_present", sa.Boolean(), nullable=True))
    op.add_column(_TABLE, sa.Column("swarm_room_tag", sa.Text(), nullable=True))
    for name in (*_CHUNK_SOURCE_COUNTERS, *_SWARM_COUNTERS):
        op.add_column(_TABLE, sa.Column(name, sa.BigInteger(), nullable=True))
    # JSONB on Postgres, JSON on SQLite -- the variant the model declares, and
    # the same shape `first_reads` and `seeds` already use on this table.
    op.add_column(
        _TABLE,
        sa.Column(
            "swarm_rooms",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "swarm_rooms")
    for name in reversed((*_CHUNK_SOURCE_COUNTERS, *_SWARM_COUNTERS)):
        op.drop_column(_TABLE, name)
    op.drop_column(_TABLE, "swarm_room_tag")
    op.drop_column(_TABLE, "swarm_present")
    for name in reversed(_MS_COLUMNS):
        op.drop_column(_TABLE, name)
