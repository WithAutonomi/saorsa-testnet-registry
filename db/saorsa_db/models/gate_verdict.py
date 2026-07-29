from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from saorsa_db.db import Base

# `overall` values. Ordered by severity: a hard-gate failure blocks the ship
# with no override, a soft-gate failure may be overridden via the recorded path
# (RM justification + one engineer co-sign + a P1 debt issue that blocks the
# next train's cut), and GRAY is RM judgment recorded either way.
OVERALL_PASS = "PASS"
OVERALL_GRAY = "GRAY"
OVERALL_SOFT_FAIL = "SOFT FAIL"
OVERALL_HARD_FAIL = "HARD FAIL"


class DeploymentGateVerdict(Base):
    """One deterministic gate comparison of a run against a baseline run.

    The release process requires a tool -- not an agent, not a human -- to
    compute the staging verdict, and requires the same inputs to always produce
    an identical report. This table is where that verdict lands so it survives
    the run: Elasticsearch and InfluxDB are per-run scratch and die with the
    testnet, and the registry Postgres is the system of record.

    Keyed on ``(deployment_id, baseline_deployment_id, window_label)`` with a
    unique constraint so re-running a comparison **replaces in place**. The same
    rule ``deployment_derived_metrics`` uses, for the same reason: a report that
    silently picked one of several rival stored answers would be
    non-deterministic, which is precisely what this whole mechanism exists to
    prevent.

    ``threshold_config_version`` is stored rather than inferred. Thresholds live
    in versioned config so the quarterly threshold review can recalibrate them
    without a release of the tool -- which means two verdicts can legitimately
    disagree, and the version is the only thing that says why. Comparing
    verdicts computed under different threshold versions is meaningless.

    ``verdict`` holds the full machine-readable payload (per-gate results,
    deltas, noise-rule detail) and ``report_markdown`` the rendered
    fixed-format report, so the exact text attached to a release issue can be
    recovered later without re-running anything.

    Deliberately covers **only** the gates derivable from stored metrics. Data
    integrity, crashes, mixed-version chunk storage and the production canaries
    are observational, are not recorded anywhere in Postgres, and are supplied
    by the operator through the accompanying skill.
    """

    __tablename__ = "deployment_gate_verdicts"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "baseline_deployment_id",
            "window_label",
            name="uq_deployment_gate_verdicts_comparison",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # No CASCADE from the baseline side and no ORM relationship: deleting a
    # baseline deployment must not silently erase the verdicts that were
    # computed against it, since the verdict is the durable artifact and the
    # baseline row is not needed to read it.
    baseline_deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id"), nullable=False, index=True
    )

    window_label: Mapped[str] = mapped_column(String(64), nullable=False)
    overall: Mapped[str] = mapped_column(String(16), nullable=False)
    threshold_config_version: Mapped[int] = mapped_column(Integer, nullable=False)

    verdict: Mapped[dict] = mapped_column(JSON, nullable=False)
    report_markdown: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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
            "baseline_deployment_id": self.baseline_deployment_id,
            "window_label": self.window_label,
            "overall": self.overall,
            "threshold_config_version": self.threshold_config_version,
            "verdict": self.verdict,
            "report_markdown": self.report_markdown,
            "computed_at": self.computed_at.isoformat(),
        }
