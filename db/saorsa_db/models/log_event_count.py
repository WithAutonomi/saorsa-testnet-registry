from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from saorsa_db.db import Base

# `source` values. Only one today, but recorded per row rather than assumed:
# these counts come from the deployment's own Elasticsearch, which is destroyed
# at teardown, and a later mechanism (a node-side counter, a metrics endpoint)
# would produce numbers that are NOT interchangeable with a log-line tally.
SOURCE_ELASTICSEARCH = "elasticsearch"

# Every counted event belongs to a group, and the comparison evaluates the
# GROUP, not the individual patterns. Some events are logged at more than one
# layer -- `Connection attempt failed` (saorsa_core::transport_handle) and
# `connect_happy_eyeballs failed` (saorsa_transport::happy_eyeballs) tracked
# each other to within 0.2% across the DEV-02 comparison (1,415/1,415 on the
# baseline, 71,309/71,160 on the branch) -- so counting them as two gates would
# report one regression twice and read as two independent corroborating
# signals.
DEFAULT_PATTERN_SET = "connection-errors"


class DeploymentLogEventCount(Base):
    """How many times one named log string appeared in a run's node logs.

    Connection errors are evaluated on every staging run (V2-842), and the
    *count* is the measurement -- the log lines themselves are far too numerous
    to keep and die with the deployment's Elasticsearch at teardown anyway.
    Extracting the counts to Postgres while the stack is still up is the only
    way a run can be compared against a later one.

    Deliberately its own table rather than a row in
    ``deployment_derived_metrics``. That table is tall and keyed and would fit
    mechanically, but ``deployments compare`` refuses outright when the two runs'
    derived-metric ``source`` sets differ -- so an ``elasticsearch``-sourced row
    on the candidate would make it uncomparable against every baseline recorded
    before this feature existed, turning a new gate into a total refusal.

    ``count`` is the gate metric. Everything beside it exists because a bare
    count answers the wrong question:

    * ``hosts`` -- distinct hosts that emitted the string. A count without it
      cannot distinguish a fleet-wide regression from one sick node. Measured on
      DEV-02 run 502, ``Accept stream error`` looked like a 2.18x regression
      until the host spread showed 2 hosts of 72, both explained by a service
      restart.
    * ``observed_start``/``observed_end`` -- the span the counted documents
      actually cover, so a count can be normalised to a rate. Windows differ
      between runs and a longer run logs more of everything.
    * ``total_documents`` -- every node log line forwarded in the same query
      scope, so "more errors" can be told apart from "more logging".
    * ``pattern`` -- the literal matched, stored per row because a count is
      uninterpretable without knowing what was counted, and the catalogue is
      versioned and will change.

    Keyed ``(deployment_id, event_key, window_label)``: re-recording replaces in
    place, matching ``deployment_derived_metrics`` and for the same reason -- two
    rival counts for one window would make a comparison non-deterministic.
    """

    __tablename__ = "deployment_log_event_counts"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "event_key",
            "window_label",
            name="uq_deployment_log_event_counts_window",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Stable key from the pattern catalogue, e.g. "connection_attempt_failed".
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Which group the comparison evaluates this under. Several keys may share
    # one group when they are the same event logged at different layers.
    event_group: Mapped[str] = mapped_column(String(64), nullable=False)
    # Which catalogue file the key came from, so counts taken under a future
    # second catalogue never silently pool with these.
    pattern_set: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_PATTERN_SET
    )
    # The catalogue's version at capture time. Same contract as the threshold
    # config's: comparing counts captured under different pattern versions is
    # only meaningful if the pattern itself did not change.
    pattern_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # The literal phrase matched. Not the paraphrase people write in an issue:
    # `[STEP 1b] {} -> {}: dial failed for all {} candidate address(es)`
    # interpolates a count, so only a stable substring can ever match.
    pattern: Mapped[str] = mapped_column(String(512), nullable=False)

    window_label: Mapped[str] = mapped_column(
        String(64), nullable=False, default="full-run"
    )

    # BigInteger deliberately: a staging fleet at DEBUG has produced hundreds of
    # millions of lines, and a single string reaching 2^31 is not far-fetched.
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    hosts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_documents: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # The span of the documents counted, NOT the operator's requested window.
    # Nullable because a pattern with zero matches has no span -- and a zero
    # count is a real, useful measurement, so it must still be storable.
    observed_start: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    observed_end: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SOURCE_ELASTICSEARCH
    )
    # Per-level and per-host breakdowns, the index pattern queried, and any
    # window filter applied. JSON so the shape can grow without a migration --
    # the same trade-off `deployment_host_results.host_metrics` makes.
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "event_key": self.event_key,
            "event_group": self.event_group,
            "pattern_set": self.pattern_set,
            "pattern_version": self.pattern_version,
            "pattern": self.pattern,
            "window_label": self.window_label,
            "count": self.count,
            "hosts": self.hosts,
            "total_documents": self.total_documents,
            "observed_start": (
                self.observed_start.isoformat() if self.observed_start else None
            ),
            "observed_end": (
                self.observed_end.isoformat() if self.observed_end else None
            ),
            "source": self.source,
            "details": self.details,
            "recorded_at": self.recorded_at.isoformat(),
        }
