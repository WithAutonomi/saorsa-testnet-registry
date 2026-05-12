import enum
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base

if TYPE_CHECKING:
    from saorsa_db.models.file_result import DeploymentFileResult
    from saorsa_db.models.host_result import DeploymentHostResult
    from saorsa_db.models.prod_canary import DeploymentProdCanaryUpload
    from saorsa_db.models.prod_upload_result import DeploymentProdUploadResult


class DeploymentStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[DeploymentStatus] = mapped_column(
        Enum(DeploymentStatus, values_callable=lambda e: [m.value for m in e]),
        default=DeploymentStatus.PENDING,
        nullable=False,
    )
    workflow_run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    deploy_args: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    events: Mapped[list["DeploymentEvent"]] = relationship(
        back_populates="deployment",
        order_by="DeploymentEvent.created_at",
        cascade="all, delete-orphan",
    )

    file_results: Mapped[list["DeploymentFileResult"]] = relationship(
        back_populates="deployment",
        order_by="DeploymentFileResult.client_hostname",
        cascade="all, delete-orphan",
    )

    host_result: Mapped[Optional["DeploymentHostResult"]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
        uselist=False,
    )

    prod_upload_results: Mapped[list["DeploymentProdUploadResult"]] = relationship(
        back_populates="deployment",
        order_by="DeploymentProdUploadResult.client_hostname",
        cascade="all, delete-orphan",
    )

    prod_canary_uploads: Mapped[list["DeploymentProdCanaryUpload"]] = relationship(
        back_populates="deployment",
        order_by="DeploymentProdCanaryUpload.uploaded_at",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "workflow_run_id": self.workflow_run_id,
            "deploy_args": self.deploy_args,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "events": [event.to_dict() for event in self.events],
        }


class DeploymentEvent(Base):
    __tablename__ = "deployment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    deployment: Mapped["Deployment"] = relationship(back_populates="events")

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "event_type": self.event_type,
            "details": self.details,
            "created_at": self.created_at.isoformat(),
        }
