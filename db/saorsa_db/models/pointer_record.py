"""Per-operation pointer results (V2-1339).

One row per `ant pointer` operation a pointer client performed: creates,
updates, the reads that confirm them, the sweeps and the chain resolutions.

A table of its own rather than a third `deployment_*_records` sibling, because a
pointer operation is not a transfer. There is no file, so `file_size_bytes` is
meaningless; and the outcome is not success-or-failure but one of seven classes,
of which `stale` is a routine, expected event on a healthy network (ADR-0016: an
eclipsed reader can be handed an older, validly signed state and cannot tell).
Folding that into `OpRecordStatus.FAILURE` would put the expected stale-read rate
into the upload/download hard floors.

Windowable by `finished_at`, like the file-op tables, so a labelled phase of a
run can be re-derived after the monitoring stack is destroyed.
"""

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class PointerOp(enum.Enum):
    """Which `ant pointer` subcommand produced the row.

    `get` is the read that immediately confirms a write (read-your-writes);
    `sweep` is a periodic read of every known pointer, and is the one that
    carries a meaningful age. Separating them matters: pooling the two would let
    a high rate of fresh confirming reads mask decay in the aged population.

    The last four are the handover ops (V2-1354). `TRANSFER` is the only paid one
    of them: a `TAKEBACK` is an attempted write that is refused BEFORE it pays,
    and `FINALITY` and `CONTROLLER` are free reads of the close group. They are
    neither writes nor reads for the success-rate denominators -- a take-back
    whose SUCCESS is a refusal cannot be averaged with writes that are supposed
    to land.
    """

    CREATE = "create"
    UPDATE = "update"
    GET = "get"
    SWEEP = "sweep"
    RESOLVE = "resolve"
    TRANSFER = "transfer"
    FINALITY = "finality"
    CONTROLLER = "controller"
    TAKEBACK = "takeback"


class PointerOutcome(enum.Enum):
    """How the operation ended.

    Seven classes rather than success/failure, because four of the six failure
    modes are ordinary events that say different things:

    - ``STALE``: the network served a state older than the one we wrote (or a
      foreign counter moved backwards). EXPECTED at a non-zero rate. Only the
      trend is a signal.
    - ``LOST_WRITE``: the update was refused because the pointer moved while it
      was in flight, or the write fell short of its quorum. Paid for, not landed.
    - ``NOT_FOUND``: the close group agreed there is no pointer there. An
      ANSWER, which is why `ant pointer get`'s exit-1-on-absent maps here and not
      to ``ERROR``.
    - ``READ_SHORTFALL``: too few of the close group answered, or no state had
      enough peers behind it. The network declined to say, rather than failing.
    - ``ANOMALY``: a counter ABOVE the one we wrote. Only a write by something
      else to a pointer this client owns could cause it, so it is never routine.
    - ``ERROR``: everything else -- a protocol or RPC failure.

    Three more come from the handover's finality check (V2-1354), and only
    ``final`` is ``OK`` there:

    - ``SETTLING``: the close group has the final state but has not converged on
      it yet. A moment in a handover's life, not a fault.
    - ``UNCONFIRMED``: final on a majority, with a peer that did not answer. The
      state a silent peer leaves behind -- and exactly what `transfer` refuses to
      write into, which is why seeing it AFTER one is worth knowing.
    - ``FORKED``: two rival final states. The one genuinely alarming finality
      outcome, and the reason it is not folded into ``ANOMALY``.
    """

    OK = "ok"
    STALE = "stale"
    LOST_WRITE = "lost_write"
    NOT_FOUND = "not_found"
    READ_SHORTFALL = "read_shortfall"
    ANOMALY = "anomaly"
    ERROR = "error"
    SETTLING = "settling"
    UNCONFIRMED = "unconfirmed"
    FORKED = "forked"


#: Outcomes that count as the operation having worked. Everything else is a
#: failure for the success-rate denominators -- including ``STALE``, which is
#: expected but is still not the state the client wrote.
POINTER_SUCCESS_OUTCOMES = (PointerOutcome.OK,)

#: The paid operations. Reads cost nothing, so a cost-per-write figure is taken
#: over these alone. ``TRANSFER`` is here because it pays -- it signs a state at
#: `u64::MAX` and buys a quote for it like any other write. ``TAKEBACK`` is not:
#: its whole point is that it is refused before paying, so counting it would put
#: a deliberate zero into the mean cost per write.
POINTER_WRITE_OPS = (PointerOp.CREATE, PointerOp.UPDATE, PointerOp.TRANSFER)


class DeploymentPointerRecord(Base):
    __tablename__ = "deployment_pointer_records"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "start_ns",
            name="uq_deployment_pointer_records_op",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: Simulated residential link this VM ran under, or NULL for an unshaped
    #: cloud link. Denormalised for the same reason as on the file-op tables.
    network_profile: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    op: Mapped[PointerOp] = mapped_column(
        Enum(PointerOp, values_callable=lambda e: [m.value for m in e]), nullable=False
    )
    outcome: Mapped[PointerOutcome] = mapped_column(
        Enum(PointerOutcome, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    #: The pointer this operation was about. Never NULL in practice, but nullable
    #: because a keygen failure produces an op with no address to name.
    pointer_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: Whether this client owns the pointer. Foreign pointers are swept too --
    #: cross-client reads are the whole reason two clients are deployed in
    #: geographically separate regions -- and the two populations are reported
    #: separately, because only for an owned pointer is the expected state known.
    owned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Numeric(20) rather than BigInteger: a pointer counter is a Rust `u64` and
    #: a handover writes `u64::MAX` (18446744073709551615), which is about twice
    #: what a signed BIGINT holds. Widened with the handover work (V2-1354);
    #: every pre-existing value is small and unaffected.
    counter_expected: Mapped[Optional[int]] = mapped_column(
        Numeric(20, 0), nullable=True
    )
    counter_observed: Mapped[Optional[int]] = mapped_column(
        Numeric(20, 0), nullable=True
    )
    #: `chunk` or `pointer` -- what the pointer was aimed at. A `pointer` target
    #: is a chain hop, and is what `resolve` follows.
    target_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: Seconds between the pointer becoming known to this client and this read.
    #: The axis decay with age is measured on, so it is stored per op rather than
    #: derived: a client restart, a re-run and a destroyed monitoring stack all
    #: make it unrecoverable afterwards.
    age_secs: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    #: On a write, how the confirming read went (a :class:`PointerOutcome` value,
    #: or empty when there was no read). This is the read-your-writes signal.
    read_back: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    #: `ant` invocations this operation made, and how many of them spent ANT. A
    #: `paid_attempts` above 1 is the ADR's double payment: there is no recovery
    #: journal, so a retried write pays a second time.
    attempts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    paid_attempts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    #: Measured spend, not a quote: `ant pointer create --json` reports no cost,
    #: so these are wallet balance deltas either side of the write. Numeric(78)
    #: because an atto balance exceeds every integer type Postgres has.
    ant_spend_atto: Mapped[Optional[int]] = mapped_column(Numeric(78, 0), nullable=True)
    gas_spend_wei: Mapped[Optional[int]] = mapped_column(Numeric(78, 0), nullable=True)

    error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # --- handover columns (V2-1354). All nullable, no backfill: the Influx
    # points behind every pre-handover row never carried these fields.
    #: The pointer a `transfer` handed ownership to, and the address the
    #: redirect read and the `controller` walk are checked against.
    recipient: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    #: `ant pointer finality`'s verbatim status -- `open` / `settling` / `final`
    #: / `unconfirmed` / `forked`. Stored as well as being mapped onto `outcome`
    #: so a future status this build maps to ``ERROR`` is still legible.
    finality_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    #: How many of the close group answered the finality check, and how many
    #: there are. `answered < group_size` is what makes a `final` reading
    #: `unconfirmed`.
    answered: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    group_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: Peers holding the state this client wrote.
    holders: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    #: Which take-back was tried: `update` or `transfer`. Both must be refused,
    #: and they are refused by different code paths, so they are reported apart.
    attempt: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    start_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    influx_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    deployment: Mapped["Deployment"] = relationship(  # noqa: F821
        back_populates="pointer_records"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "client_hostname": self.client_hostname,
            "region": self.region,
            "network_profile": self.network_profile,
            "op": self.op.value,
            "outcome": self.outcome.value,
            "pointer_address": self.pointer_address,
            "owned": self.owned,
            "counter_expected": (
                int(self.counter_expected)
                if self.counter_expected is not None
                else None
            ),
            "counter_observed": (
                int(self.counter_observed)
                if self.counter_observed is not None
                else None
            ),
            "target_kind": self.target_kind,
            "age_secs": self.age_secs,
            "read_back": self.read_back,
            "attempts": self.attempts,
            "paid_attempts": self.paid_attempts,
            "ant_spend_atto": (
                str(self.ant_spend_atto) if self.ant_spend_atto is not None else None
            ),
            "gas_spend_wei": (
                str(self.gas_spend_wei) if self.gas_spend_wei is not None else None
            ),
            "error": self.error,
            "recipient": self.recipient,
            "finality_status": self.finality_status,
            "answered": self.answered,
            "group_size": self.group_size,
            "holders": self.holders,
            "attempt": self.attempt,
            "start_ns": self.start_ns,
            "influx_time": self.influx_time.isoformat(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "recorded_at": self.recorded_at.isoformat(),
        }
