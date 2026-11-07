"""Per-round browser-download records (V2-1618).

One row per Chromium instance per round of a `browser_downloaders` client tier
(V2-1617): N real headless browsers per VM, all reading one operator-chosen hot
file on a shared wall-clock schedule, which is the only instrument that can
measure what ADR-0023 (hot-record read spreading) changes.

A table of its own rather than more rows in ``deployment_download_records``, for
the same reason the harness writes its own ``browser_download`` Influx
measurement rather than the shared ``download`` one: a synchronised cohort
hammering ONE file and a single client drawing random files are two unlike
workloads, and pooling them would let any success rate or percentile average the
two. It also carries fields no file transfer has -- the round it belongs to, the
instance inside the VM, and the three per-dial summaries derived from the
browser's own trace.

Windowable by ``started_at``, which is exactly the round's boundary (the harness
stamps ``start_ns`` as ``round_id x period``, not as the instant the browser
happened to launch), so a labelled phase of a run can be re-derived after the
VMs and their InfluxDB are gone.

The RAW per-dial records are deliberately not here and never were: the harness
forwards them to the run's Elasticsearch as they are produced (V2-1650), where
`analysis browser-reads` reads them to work out which holder served each chunk's
first read. A run is ~8 million of them, which is a log, not a registry table.
"""

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base
from saorsa_db.models.hot_file_download_record import SWARM_COUNTER_COLUMNS
from saorsa_db.models.op_record import OpRecordStatus

#: Where each chunk of a round came from, as the harness parsed it out of the
#: bundle's own download progress messages: a node's peer id, the literal
#: `cache`, or the literal `a reader`. A different source from the twenty-five
#: counters below -- the progress lines rather than `swarmStats()` -- so a bundle
#: can report one and not the other, which is why they are two tuples.
CHUNK_SOURCE_COLUMNS = (
    "chunks_total",
    "chunks_from_nodes",
    "chunks_from_cache",
    "chunks_from_readers",
)

#: The twenty-five swarm counters, **imported** from the hot-file record rather
#: than re-declared: the native and browser cohorts of one run measure the same
#: thing, and two lists would be two vocabularies the moment one of them gained
#: a counter.
SWARM_COLUMNS = SWARM_COUNTER_COLUMNS


class BrowserDownloadErrorKind(str, enum.Enum):
    """The ``error_kind`` values the harness's classifier produces.

    A plain string column in the table rather than an enum type: the classifier
    lives in the browser harness (`lib/classify.mjs`) and a kind it gains must
    be recordable without a database migration standing in the way of a run.
    These constants exist so the report's gate on ``not_found`` names the same
    string the harness writes.
    """

    BUSY = "busy"
    NOT_FOUND = "not_found"
    PAYMENT = "payment"
    QUORUM = "quorum"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    OTHER = "other"
    #: Emitted on a round that ran before the operator selected a hot file. A
    #: `skipped` row, and part of the series rather than a failure.
    NO_HOT_FILES = "no_hot_files"
    #: An instance was still running the previous round when this one came due.
    ROUND_OVERRUN = "round_overrun"


class DeploymentBrowserDownloadRecord(Base):
    """One instance's download in one round.

    ``OpRecordStatus`` is reused rather than a browser-specific enum declared:
    the three values are exactly ``success`` / ``failure`` / ``skipped`` and they
    mean exactly what they mean on a file op, including the load-bearing part --
    a ``skipped`` row is STORED but excluded from every denominator. Declaring a
    second Postgres enum type with the same three labels would only invite the
    two to drift.
    """

    __tablename__ = "deployment_browser_download_records"
    __table_args__ = (
        # One point per instance per round, always -- including the rounds that
        # did nothing -- so this is the operation's identity and a re-run of
        # `record-results` revises rather than duplicates.
        UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "round_id",
            "instance",
            name="uq_deployment_browser_download_records_round",
        ),
    )

    # BIGINT on Postgres; INTEGER on SQLite, where only an `INTEGER PRIMARY KEY`
    # aliases the rowid and so autoincrements -- a BIGINT one refuses every
    # insert that does not name an id, which would make the table untestable
    # against the SQLite fixtures. Postgres DDL is unchanged.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True
    )
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    # The `kind: clients` extension whose tier this VM belongs to, or NULL for a
    # VM declared inside the deployment's own manifest. A browser cohort is
    # normally added as an extension (a hot file only exists once the
    # deployment's uploaders have published one), so without this the rows of a
    # cohort added at hour 3 would be indistinguishable from the deployment's
    # own.
    extension: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    region: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # `round_id = floor(epoch_secs / round_period_secs)`. BIGINT because it is
    # an epoch second divided by a period, which outgrows INTEGER for any period
    # under ~68 years.
    round_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    instance: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Which of the selected hot files this round read (`round_id mod len`). NULL
    # on a `skipped` row that never chose one.
    hot_file_index: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[OpRecordStatus] = mapped_column(
        Enum(
            OpRecordStatus,
            name="oprecordstatus",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    error_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    connect_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # This instance's SCHEDULED start: the harness stamps `start_ns` as
    # `(round_id x period + offset) x 1e9`, and the round's BOUNDARY is
    # `started_at - start_offset_ms` -- which is what makes a window selection
    # over these rows select whole rounds. With no stagger the offset is 0 and
    # this IS the boundary, which is every row written before INFRA-532. How
    # late the instance actually got going is `started_late_ms`, measured from
    # the scheduled start rather than from the boundary: a staggered instance is
    # not late for starting when it was told to.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_late_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # `((client_index - 1) x instances + (instance - 1)) x start_stagger_secs`,
    # in milliseconds. Recorded on the row rather than only derivable from the
    # cohort's config, because the config is what `clients provision` happened
    # to write last and this is what the round actually ran.
    start_offset_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # How long the page stayed open AFTER its download, going on serving the
    # instances that started later in the stagger (`seed_until_secs`). 0 when
    # the page closed with its download, which is every row before INFRA-532 and
    # every failed or timed-out round.
    seeded_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The bootstrap multiaddrs this instance was given, as an array. Every
    # download dials every seed, so this is also what makes the per-round
    # session load on each bootstrap computable.
    seeds: Mapped[Optional[list]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    stalls: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    refused_dials: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # `[{chunk, holder, known_holders}]` -- for each chunk this download read,
    # the holder its FIRST read went to and the discovery-reported holder count
    # for that chunk over the whole download. The busiest holder's share of
    # these is the verdict on hot-record read spreading; see V2-1617's amended
    # definition of `known_holders` (it is NOT the count at the instant of the
    # read, and `null` means no count was reported, never 0).
    first_reads: Mapped[Optional[list]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    # Whether the page's client could report a swarm at all -- that is, whether
    # the bundle has `swarmStats()`. See the module docstring on the hot-file
    # record for why the flag is load-bearing: without it a zero
    # `records_from_peers` from a pinned older bundle is indistinguishable from
    # a swarm that formed and shared nothing.
    swarm_present: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # The swarm room this download joined, from `swarmRoomStats()` or, failing
    # that, from the bundle's `Swarm room {tag}` progress line. Empty when
    # neither reported one.
    swarm_room_tag: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    chunks_total: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    chunks_from_nodes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    chunks_from_cache: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    chunks_from_readers: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )

    records_from_peers: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    bytes_from_peers: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    records_rejected: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    asks_busy: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    asks_missing: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    asks_failed: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    records_served: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    bytes_served: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    links: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    rooms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    discovery_lookups: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    discovery_announces: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_announces_failed: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_announces_timed_out: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_announces_refused: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_bytes_sent: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_bytes_received: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_offers_made: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_offers_delivered: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_offers_received: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_answers_sent: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_answers_received: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_channels_opened: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_links_proven: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    discovery_links_failed: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )

    # `swarmRoomStats()` verbatim: one entry per room this download joined, each
    # with its own counters and open links. A JSON column rather than a second
    # table, as on the hot-file record -- it is a per-operation detail read
    # alongside its row, never joined or aggregated across rows.
    swarm_rooms: Mapped[Optional[list]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    deployment: Mapped["Deployment"] = relationship(  # noqa: F821
        back_populates="browser_download_records"
    )

    def to_dict(self):
        out = {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "client_hostname": self.client_hostname,
            "extension": self.extension,
            "region": self.region,
            "round_id": self.round_id,
            "instance": self.instance,
            "hot_file_index": self.hot_file_index,
            "address": self.address,
            "file_size_bytes": self.file_size_bytes,
            "status": self.status.value,
            "error_kind": self.error_kind,
            "error_text": self.error_text,
            "total_ms": self.total_ms,
            "connect_ms": self.connect_ms,
            "started_at": self.started_at.isoformat(),
            "started_late_ms": self.started_late_ms,
            "start_offset_ms": self.start_offset_ms,
            "seeded_ms": self.seeded_ms,
            "seeds": self.seeds,
            "stalls": self.stalls,
            "refused_dials": self.refused_dials,
            "first_reads": self.first_reads,
            "swarm_present": self.swarm_present,
            "swarm_room_tag": self.swarm_room_tag,
            "swarm_rooms": self.swarm_rooms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        for column in (*CHUNK_SOURCE_COLUMNS, *SWARM_COLUMNS):
            out[column] = getattr(self, column)
        return out
