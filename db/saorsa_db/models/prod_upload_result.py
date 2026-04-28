import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class ProdUploadStatus(enum.Enum):
    SUCCESS = "success"
    PARTIAL_UPLOAD = "partial_upload"
    DOWNLOAD_FAILED = "download_failed"


class DeploymentProdUploadResult(Base):
    __tablename__ = "deployment_prod_upload_results"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "source_url",
            name="uq_deployment_prod_upload_results_group",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[ProdUploadStatus] = mapped_column(
        Enum(ProdUploadStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    chunks_stored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    upload_secs: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

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
        back_populates="prod_upload_results"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "client_hostname": self.client_hostname,
            "region": self.region,
            "source_url": self.source_url,
            "file_name": self.file_name,
            "file_size_bytes": self.file_size_bytes,
            "status": self.status.value,
            "address": self.address,
            "chunks_stored": self.chunks_stored,
            "chunks_failed": self.chunks_failed,
            "total_chunks": self.total_chunks,
            "duration_ms": self.duration_ms,
            "upload_secs": self.upload_secs,
            "reason": self.reason,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
        }
