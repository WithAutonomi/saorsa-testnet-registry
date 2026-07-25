from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from saorsa_db.db import Base

# `metric` values. These deliberately reuse the key names the whole-run rollup
# uses in `deployment_host_results.host_metrics`, so re-deriving a window
# produces rollup-shaped dicts without a translation table.
METRIC_CPU_PERCENT = "cpu_percent"
METRIC_MEM_PERCENT = "mem_percent"
METRIC_BYTES_IN_PER_SEC = "bytes_in_per_sec"
METRIC_BYTES_OUT_PER_SEC = "bytes_out_per_sec"
METRIC_READ_BYTES_PER_SEC = "read_bytes_per_sec"
METRIC_WRITE_BYTES_PER_SEC = "write_bytes_per_sec"
METRIC_READS_PER_SEC = "reads_per_sec"
METRIC_WRITES_PER_SEC = "writes_per_sec"
METRIC_RSS_BYTES = "rss_bytes"

# Metrics carried on the per-device dimension (`device` + `volume_role` set).
DISK_METRICS = frozenset(
    {
        METRIC_READ_BYTES_PER_SEC,
        METRIC_WRITE_BYTES_PER_SEC,
        METRIC_READS_PER_SEC,
        METRIC_WRITES_PER_SEC,
    }
)


class DeploymentHostMetricBucket(Base):
    """One time bucket of one host metric series.

    A tall numeric table rather than a JSON blob per bucket: re-deriving an
    arbitrary window is then a single ``GROUP BY`` filtered server-side, instead
    of pulling every bucket back and re-aggregating in Python.

    Written by ``deployments record-analysis`` alongside — not instead of — the
    whole-run rollup in ``deployment_host_results``. Deployments recorded before
    this table existed simply have no rows here; consumers must degrade to the
    rollup rather than assume buckets exist. No backfill is possible: the source
    InfluxDB for a past run is destroyed with its monitoring stack.

    **Re-aggregating ``avg`` across buckets must be sample-weighted** —
    ``sum(avg * samples) / sum(samples)``, never a mean of means. Buckets do not
    hold equal sample counts at run boundaries or where scrapes were missed.
    ``min``/``max``/``samples`` compose trivially. See
    ``SqlAlchemyDeploymentRepository.get_host_metric_window``.

    Follows ``prod_canary_probes`` for a high-row-count table: ``BigInteger`` PK,
    a DB-level ``ON DELETE CASCADE`` and deliberately **no** ORM relationship
    back on ``Deployment`` — an ORM ``delete-orphan`` cascade would load every
    row into the session just to delete a deployment.
    """

    __tablename__ = "deployment_host_metric_buckets"
    __table_args__ = (
        # The only index, deliberately. Every read filters on a deployment and a
        # `bucket_start` range and groups by host/metric afterwards, so an index
        # on (deployment_id, host, metric) would do no work for it while costing
        # insert time and disk on every row -- and the registry runs on the
        # smallest managed Postgres tier. Add one when a query needs it.
        Index(
            "ix_deployment_host_metric_buckets_window",
            "deployment_id",
            "bucket_start",
        ),
    )

    # BIGINT on Postgres -- a year of weekly runs is millions of rows. SQLite
    # only autoincrements a plain INTEGER PRIMARY KEY (the rowid alias), so the
    # in-memory test database needs the variant or every insert fails on a NULL
    # id.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
    )

    host: Mapped[str] = mapped_column(String(255), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(64), nullable=False)

    # Set for the four diskio series only. Bucketed disk I/O is restricted to
    # the device tagged `node-storage`: on a typical VM the other devices are
    # the root disk's boot/ESP partitions, whose counters merely duplicate the
    # whole-disk counters. Every device is still kept in the rollup JSON.
    device: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    volume_role: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Set for `rss_bytes` only: the telegraf `node_service` tag ("ant-node-3").
    # Keyed by service, never by PID -- services restart and PIDs churn, and a
    # host runs 15 of them.
    service: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "host": self.host,
            "bucket_start": self.bucket_start.isoformat(),
            "metric": self.metric,
            "device": self.device,
            "volume_role": self.volume_role,
            "service": self.service,
            "avg": self.avg,
            "min": self.min,
            "max": self.max,
            "samples": self.samples,
            "recorded_at": self.recorded_at.isoformat(),
        }
