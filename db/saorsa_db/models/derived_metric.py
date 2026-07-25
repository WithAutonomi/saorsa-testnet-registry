from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from saorsa_db.db import Base

# Canonical metric keys. The two ratios are the gate metrics from the rc.1
# verdict; everything else is a component one of them is built from, stored in
# its own right so an odd ratio is debuggable and so a later gate can normalise
# differently without a re-extraction (the source InfluxDB will be gone).
METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED = "outbound_bytes_per_gb_uploaded"
METRIC_OUTBOUND_BYTES_PER_GB_TRAFFIC = "outbound_bytes_per_gb_traffic"
METRIC_DISK_BYTES_PER_GB_STORED = "disk_bytes_per_gb_stored"

METRIC_NODE_OUTBOUND_BYTES_TOTAL = "node_outbound_bytes_total"
METRIC_NODE_INBOUND_BYTES_TOTAL = "node_inbound_bytes_total"
METRIC_NODE_STORAGE_GROWTH_BYTES = "node_storage_growth_bytes"
METRIC_GB_UPLOADED = "gb_uploaded"
METRIC_GB_DOWNLOADED = "gb_downloaded"

RATIO_METRICS = (
    METRIC_OUTBOUND_BYTES_PER_GB_UPLOADED,
    METRIC_OUTBOUND_BYTES_PER_GB_TRAFFIC,
    METRIC_DISK_BYTES_PER_GB_STORED,
)

COMPONENT_METRICS = (
    METRIC_NODE_OUTBOUND_BYTES_TOTAL,
    METRIC_NODE_INBOUND_BYTES_TOTAL,
    METRIC_NODE_STORAGE_GROWTH_BYTES,
    METRIC_GB_UPLOADED,
    METRIC_GB_DOWNLOADED,
)

# `window_label` for the whole recorded run -- the default a comparison uses
# unless it deliberately slices a phase out.
WINDOW_FULL_RUN = "full-run"

# `source` values.
#
# The distinction is load-bearing, not bookkeeping: a whole-run capture sums
# raw counter deltas and is exact, while a window re-derived from 5-minute
# buckets integrates averaged rates and is approximate. Comparing one against
# the other as though they were the same measurement is precisely the kind of
# silent error this table exists to prevent.
SOURCE_CAPTURE = "capture"
SOURCE_BUCKETS = "buckets"


class DeploymentDerivedMetric(Base):
    """One canonical, normalised metric for one deployment over one window.

    Raw measurements alone are not comparable across runs: a longer run moves
    more bytes, and a run with a different client mix stores a different amount
    of data. The rc.1 gate verdict recorded "outbound bytes per GB of user
    traffic" and "disk consumed per GB stored" as NOT ASSESSED for exactly that
    reason. These rows are the normalised values, computed at extraction time
    and stored beside the raw data they came from.

    Deliberately a tall, keyed table rather than typed columns per metric: the
    comparison-report command adds gate metrics over time, and each one should
    not need a migration. The cost is that a consumer must know a metric's key,
    which is what the module-level constants are for.

    ``numerator``/``denominator`` are kept alongside ``value`` because a ratio
    on its own cannot be sanity-checked -- and because a later gate may want to
    renormalise, which is impossible once the source InfluxDB is destroyed.
    Component rows (bytes and GB totals) carry only ``value``.

    ``details`` holds the per-metric breakdown (contributing host count, hosts
    missing a series, min/max spread, and so on). It is JSON so the shape can
    vary per metric without a migration -- the same trade-off
    ``deployment_host_results.host_metrics`` makes.
    """

    __tablename__ = "deployment_derived_metrics"
    __table_args__ = (
        # One value per metric per window per deployment. Recomputing a window
        # (a fixed bug, a longer run) must update in place rather than
        # accumulate rival answers -- a baseline comparison that silently picked
        # one of several rows would be non-deterministic, which is the opposite
        # of what the comparison-report command needs.
        UniqueConstraint(
            "deployment_id",
            "metric",
            "window_label",
            name="uq_deployment_derived_metrics_window",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    # "full-run", or an operator-chosen phase name such as "steady-state".
    window_label: Mapped[str] = mapped_column(
        String(64), nullable=False, default=WINDOW_FULL_RUN
    )
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    window_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    numerator: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    denominator: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # "bytes_per_gb", "bytes", "gb" -- what `value` is measured in, so a report
    # can format it without a lookup table keyed on the metric name.
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)

    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "metric": self.metric,
            "window_label": self.window_label,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "unit": self.unit,
            "source": self.source,
            "details": self.details,
            "computed_at": self.computed_at.isoformat(),
        }
