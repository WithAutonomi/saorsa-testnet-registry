import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class FileOpType(enum.Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"


class DeploymentFileResult(Base):
    __tablename__ = "deployment_file_results"
    __table_args__ = (
        UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "op_type",
            "file_size_bytes",
            name="uq_deployment_file_results_group",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    op_type: Mapped[FileOpType] = mapped_column(
        Enum(FileOpType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    first_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    avg_duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    min_duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    max_duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    deployment: Mapped["Deployment"] = relationship(back_populates="file_results")  # noqa: F821

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "client_hostname": self.client_hostname,
            "op_type": self.op_type.value,
            "file_size_bytes": self.file_size_bytes,
            "first_started_at": self.first_started_at.isoformat(),
            "last_finished_at": self.last_finished_at.isoformat(),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "avg_duration_ms": self.avg_duration_ms,
            "min_duration_ms": self.min_duration_ms,
            "max_duration_ms": self.max_duration_ms,
            "recorded_at": self.recorded_at.isoformat(),
        }
