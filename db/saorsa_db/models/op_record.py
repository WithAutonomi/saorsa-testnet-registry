import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class OpRecordStatus(enum.Enum):
    SUCCESS = "success"
    FAILURE = "failure"


class _BaseOpRecord:
    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[OpRecordStatus] = mapped_column(
        Enum(OpRecordStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
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

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "client_hostname": self.client_hostname,
            "region": self.region,
            "file_size_bytes": self.file_size_bytes,
            "status": self.status.value,
            "start_ns": self.start_ns,
            "influx_time": self.influx_time.isoformat(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
            "recorded_at": self.recorded_at.isoformat(),
        }


class DeploymentUploadRecord(_BaseOpRecord, Base):
    __tablename__ = "deployment_upload_records"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "start_ns",
            name="uq_deployment_upload_records_op",
        ),
    )

    deployment: Mapped["Deployment"] = relationship(  # noqa: F821
        back_populates="upload_records"
    )


class DeploymentDownloadRecord(_BaseOpRecord, Base):
    __tablename__ = "deployment_download_records"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "start_ns",
            name="uq_deployment_download_records_op",
        ),
    )

    deployment: Mapped["Deployment"] = relationship(  # noqa: F821
        back_populates="download_records"
    )
