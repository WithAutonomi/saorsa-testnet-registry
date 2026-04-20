from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    JSON,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class DeploymentHostResult(Base):
    __tablename__ = "deployment_host_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    host_metrics: Mapped[list] = mapped_column(JSON, nullable=False)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    metrics_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    metrics_ended_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    deployment: Mapped["Deployment"] = relationship(back_populates="host_result")  # noqa: F821

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "host_metrics": self.host_metrics,
            "summary": self.summary,
            "metrics_started_at": self.metrics_started_at.isoformat(),
            "metrics_ended_at": self.metrics_ended_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
        }
