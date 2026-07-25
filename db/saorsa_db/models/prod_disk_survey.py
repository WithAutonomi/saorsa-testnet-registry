from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class ProdDiskSurvey(Base):
    """One fleet-wide disk-pressure survey run.

    Standalone (no ``deployment_id`` FK) — production node fleets are driven by
    ``inventory.json``, not a ``Deployment`` row, so surveys are keyed by the
    inventory identifier string (e.g. ``ant-prod-01``), mirroring the
    ``migrations`` table. Per-service sizes live in the child
    ``prod_disk_survey_services`` rows; per-host free-disk (low cardinality) is
    a JSON blob keyed by host name.
    """

    __tablename__ = "prod_disk_surveys"

    id: Mapped[int] = mapped_column(primary_key=True)
    inventory_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    surveyed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # {host: {"provider": str, "total_bytes": int, "used_bytes": int, "free_bytes": int}}
    host_disk: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    host_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    service_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_data_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    services: Mapped[list["ProdDiskSurveyService"]] = relationship(
        back_populates="survey",
        order_by="ProdDiskSurveyService.size_bytes",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "inventory_name": self.inventory_name,
            "surveyed_at": self.surveyed_at.isoformat(),
            "host_disk": self.host_disk,
            "host_count": self.host_count,
            "service_count": self.service_count,
            "total_data_bytes": self.total_data_bytes,
            "recorded_at": self.recorded_at.isoformat(),
            "services": [s.to_dict() for s in self.services],
        }


class ProdDiskSurveyService(Base):
    """One (host, service) row within a survey — the node service and the bytes
    its data dir was using at ``surveyed_at``. Ordered ascending by size for
    deletion-candidate ranking."""

    __tablename__ = "prod_disk_survey_services"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True
    )
    survey_id: Mapped[int] = mapped_column(
        ForeignKey("prod_disk_surveys.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    service_name: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    survey: Mapped["ProdDiskSurvey"] = relationship(back_populates="services")

    def to_dict(self):
        return {
            "id": self.id,
            "survey_id": self.survey_id,
            "host": self.host,
            "provider": self.provider,
            "service_name": self.service_name,
            "size_bytes": self.size_bytes,
            "active": self.active,
            "enabled": self.enabled,
        }
