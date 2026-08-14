import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
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
    # Name of the simulated residential network profile this client VM ran
    # under (V2-733), or NULL for an unshaped cloud link — which is every row
    # predating the feature. Denormalised onto each op so a run's transfers can
    # be filtered and compared across trains without joining through
    # deploy_args JSON; the full resolved knob set (rates, delay, loss, profile
    # set version) lives once in the deployment's deploy_args.
    network_profile: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[ProdUploadStatus] = mapped_column(
        Enum(ProdUploadStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # SHA-256 of the exact bytes handed to `ant`, computed by the uploader
    # itself (V2-837). This is what an independent verifier compares its
    # download against, so read-back verification does not depend on the pool
    # entry carrying a catalogued digest — many carry only md5/sha1, some
    # none. NULL on every row predating the daily write test.
    sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    chunks_stored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    upload_secs: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # On-chain cost of the upload, surfaced by `ant --json file upload`. Stored
    # as NUMERIC (uint256-shaped) so large values keep full precision; both
    # nullable since older `ant` binaries / already-recorded rows omit them.
    storage_cost_atto: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(), nullable=True
    )
    gas_cost_wei: Mapped[Optional[Decimal]] = mapped_column(Numeric(), nullable=True)

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
            "network_profile": self.network_profile,
            "region": self.region,
            "source_url": self.source_url,
            "file_name": self.file_name,
            "file_size_bytes": self.file_size_bytes,
            "status": self.status.value,
            "address": self.address,
            "sha256": self.sha256,
            "chunks_stored": self.chunks_stored,
            "chunks_failed": self.chunks_failed,
            "total_chunks": self.total_chunks,
            "duration_ms": self.duration_ms,
            "upload_secs": self.upload_secs,
            "reason": self.reason,
            "storage_cost_atto": (
                str(self.storage_cost_atto)
                if self.storage_cost_atto is not None
                else None
            ),
            "gas_cost_wei": (
                str(self.gas_cost_wei) if self.gas_cost_wei is not None else None
            ),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
        }
