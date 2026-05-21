import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class ProdDownloadStatus(enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"


class DeploymentProdDownloadResult(Base):
    __tablename__ = "deployment_prod_download_results"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "address",
            name="uq_deployment_prod_download_results_group",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    address: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[ProdDownloadStatus] = mapped_column(
        Enum(ProdDownloadStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    output_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    download_secs: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    deployment: Mapped["Deployment"] = relationship(  # noqa: F821
        back_populates="prod_download_results"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "client_hostname": self.client_hostname,
            "region": self.region,
            "address": self.address,
            "status": self.status.value,
            "output_path": self.output_path,
            "file_size_bytes": self.file_size_bytes,
            "duration_ms": self.duration_ms,
            "download_secs": self.download_secs,
            "reason": self.reason,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
        }
