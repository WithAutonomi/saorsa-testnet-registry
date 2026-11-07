"""Per-round hot-file reader records (INFRA-531).

One row per native `ant file download` reader per round of a
`hot_file_readers` client tier: N readers per VM, all reading the one file
`clients select-hot-files` chose, each starting at its own offset after a
wall-clock round boundary. It is the instrument ADR-0026 (the peer swarm:
readers of a file share its records with each other over WebRTC data channels,
with the nodes introducing them and staying the last seed) is measured with,
because a swarm only forms when several readers read the SAME file at the SAME
time.

A table of its own rather than more rows in ``deployment_download_records``, for
the same reason the reader writes its own ``hot_file_download`` Influx
measurement rather than the shared ``download`` one: a synchronised cohort
hammering ONE file and a single client drawing random files are two unlike
workloads, and pooling them would let any success rate or percentile average the
two — and would put the hot-file cohort's failures into the native hard floors
`deployments compare` gates on. It also carries fields no file transfer has: the
round it belongs to, the reader inside the VM, and the twenty-five swarm
counters the CLI prints.

Windowable by ``started_at``, which is exactly the reader's SCHEDULED start
(``round_id × period + offset``), so a labelled phase of a run can be
re-derived after the VMs and their InfluxDB are gone.

**``swarm_present`` is the column that makes a zero readable.** A released `ant`
(0.3.13) prints the same result object without ``swarm`` and ``swarm_rooms``, so
a control cohort's rows carry zeros throughout — and without this flag a zero
``records_from_peers`` from a released binary would be indistinguishable from a
swarm that formed and shared nothing.
"""

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
from saorsa_db.models.op_record import OpRecordStatus

#: The swarm counters carried on every row, in the order the reader records
#: them. Named here rather than only as columns so the repository's upsert and
#: the `record-results` pass cannot each keep their own list — a counter in one
#: and not the other is a column that silently reads NULL forever.
SWARM_COUNTER_COLUMNS = (
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


class DeploymentHotFileDownloadRecord(Base):
    """One reader's download in one round.

    ``OpRecordStatus`` is reused rather than a role-specific enum declared: the
    three values are exactly ``success`` / ``failure`` / ``skipped`` and they
    mean exactly what they mean on a file op, including the load-bearing part --
    a ``skipped`` row is STORED but excluded from every denominator.
    """

    __tablename__ = "deployment_hot_file_download_records"
    __table_args__ = (
        # One point per reader per round, always -- including the rounds that
        # did nothing -- so this is the operation's identity and a re-run of
        # `record-results` revises rather than duplicates.
        #
        # **``started_at`` is part of it, and that is what makes the identity
        # SCHEDULE-QUALIFIED.** A `round_id` is `floor(epoch / period)`, so the
        # same number names a different instant under a different period --
        # recreate a cohort under the same name and hostname with 86401 s
        # instead of 86400 and round 20833 is 2027-01-15 00:00:00 on the first
        # schedule and 05:47:13 on the second. Both schedules validate, so both
        # reads are real; on the four-column key they collided and the later
        # one silently destroyed the earlier. `started_at` is the reader's
        # SCHEDULED start, derived from `round_id x period + offset`, so it is
        # stable for a given (reader, round, schedule) -- a `record-results`
        # re-run still revises its own row rather than inserting beside it --
        # and differs exactly when the schedule does.
        UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "round_id",
            "instance",
            "started_at",
            name="uq_deployment_hot_file_download_records_round",
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
    # VM declared inside the deployment's own manifest. A cohort is normally
    # added as an extension (a hot file only exists once the deployment's
    # uploaders have published one), and the DEV-01 run adds two -- a branch
    # build on even rounds and a released control on odd -- so without this the
    # two cohorts' rows would be indistinguishable.
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
    # Wall-clock time of the `ant` process, spawn to exit.
    total_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # `elapsed_secs × 1000` from the CLI's own result object: the download time
    # as the client measured it, which excludes the process spawn.
    client_elapsed_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The reader's SCHEDULED start (`round_id × period + offset`), not the
    # instant it actually got going -- which is what makes a window selection
    # over these rows select whole rounds. How late it actually started is
    # `started_late_ms`. Part of the unique key: see the constraint above, where
    # it is what keeps two schedules' reads of one `round_id` apart.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_late_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 0 ok, 1 mismatch, 2 nothing to check against -- the same three codes
    # `downloader.sh` emits, and 2 is NOT a failure (a pool line that published
    # no SHA-256 leaves nothing to verify, which is not a mismatch).
    verify_status: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    # Whether the result object carried a `swarm` block at all. See the module
    # docstring: this is what tells a released binary's zeros from a swarm that
    # formed and shared nothing.
    swarm_present: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

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

    # The CLI's `swarm_rooms` array verbatim: one entry per room this download
    # joined, each with its own record and byte counters and its open links. A
    # JSON column rather than a second table -- it is a per-operation detail
    # read alongside its row, never joined or aggregated across rows.
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
        back_populates="hot_file_download_records"
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
            "client_elapsed_ms": self.client_elapsed_ms,
            "started_at": self.started_at.isoformat(),
            "started_late_ms": self.started_late_ms,
            "verify_status": self.verify_status,
            "swarm_present": self.swarm_present,
            "swarm_rooms": self.swarm_rooms,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        for column in SWARM_COUNTER_COLUMNS:
            out[column] = getattr(self, column)
        return out
