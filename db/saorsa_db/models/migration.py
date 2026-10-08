import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class MigrationStatus(enum.Enum):
    PLANNING = "planning"
    INFRA_PROVISIONED = "infra_provisioned"
    NODES_PROVISIONED = "nodes_provisioned"
    SOAKING = "soaking"
    DRAINING = "draining"
    DRAINED = "drained"
    DESTROYING = "destroying"
    COMPLETE = "complete"
    FAILED = "failed"


class MigrationVmStatus(enum.Enum):
    PLANNED = "planned"
    INFRA_PROVISIONED = "infra_provisioned"
    NODES_PROVISIONED = "nodes_provisioned"
    NODES_STARTED = "nodes_started"
    SOURCE_DRAINED = "source_drained"
    SOURCE_DESTROYED = "source_destroyed"


class Migration(Base):
    __tablename__ = "migrations"
    __table_args__ = (UniqueConstraint("name", name="uq_migrations_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    deployment_name: Mapped[str] = mapped_column(String(255), nullable=False)

    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_region: Mapped[str] = mapped_column(String(64), nullable=False)

    target_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    target_server_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_volume_size_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    target_locations: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    node_count_per_vm: Mapped[int] = mapped_column(Integer, nullable=False, default=15)

    status: Mapped[MigrationStatus] = mapped_column(
        Enum(MigrationStatus, values_callable=lambda e: [m.value for m in e]),
        default=MigrationStatus.PLANNING,
        nullable=False,
    )

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
    soak_completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    vms: Mapped[list["MigrationVm"]] = relationship(
        back_populates="migration",
        order_by="MigrationVm.id",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "deployment_name": self.deployment_name,
            "source_provider": self.source_provider,
            "source_region": self.source_region,
            "target_provider": self.target_provider,
            "target_server_type": self.target_server_type,
            "target_volume_size_gb": self.target_volume_size_gb,
            "target_locations": self.target_locations,
            "node_count_per_vm": self.node_count_per_vm,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "soak_completed_at": (
                self.soak_completed_at.isoformat()
                if self.soak_completed_at is not None
                else None
            ),
            "vms": [vm.to_dict() for vm in self.vms],
        }


class MigrationVm(Base):
    __tablename__ = "migration_vms"

    id: Mapped[int] = mapped_column(primary_key=True)
    migration_id: Mapped[int] = mapped_column(
        ForeignKey("migrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_provider_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_region: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ssh_user: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ssh_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_services: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    target_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    target_provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    target_provider_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    target_location: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    target_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    target_volume_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    status: Mapped[MigrationVmStatus] = mapped_column(
        Enum(MigrationVmStatus, values_callable=lambda e: [m.value for m in e]),
        default=MigrationVmStatus.PLANNED,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    infra_provisioned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    nodes_provisioned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    nodes_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_drained_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_destroyed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    migration: Mapped["Migration"] = relationship(back_populates="vms")

    def to_dict(self):
        return {
            "id": self.id,
            "migration_id": self.migration_id,
            "source_name": self.source_name,
            "source_provider": self.source_provider,
            "source_provider_id": self.source_provider_id,
            "source_region": self.source_region,
            "source_ip": self.source_ip,
            "source_ssh_user": self.source_ssh_user,
            "source_ssh_key": self.source_ssh_key,
            "source_services": self.source_services,
            "target_name": self.target_name,
            "target_provider": self.target_provider,
            "target_provider_id": self.target_provider_id,
            "target_location": self.target_location,
            "target_ip": self.target_ip,
            "target_volume_id": self.target_volume_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "infra_provisioned_at": (
                self.infra_provisioned_at.isoformat()
                if self.infra_provisioned_at is not None
                else None
            ),
            "nodes_provisioned_at": (
                self.nodes_provisioned_at.isoformat()
                if self.nodes_provisioned_at is not None
                else None
            ),
            "nodes_started_at": (
                self.nodes_started_at.isoformat()
                if self.nodes_started_at is not None
                else None
            ),
            "source_drained_at": (
                self.source_drained_at.isoformat()
                if self.source_drained_at is not None
                else None
            ),
            "source_destroyed_at": (
                self.source_destroyed_at.isoformat()
                if self.source_destroyed_at is not None
                else None
            ),
        }
