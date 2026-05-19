import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Uuid,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class ProdCanaryInitialVerifyStatus(enum.Enum):
    OK = "ok"
    FAILED = "failed"


class ProdCanaryProbeStatus(enum.Enum):
    OK = "ok"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class ProdCanaryChunkRole(enum.Enum):
    DATA_MAP = "data_map"
    DATA = "data"


class DeploymentProdCanaryUpload(Base):
    __tablename__ = "prod_canary_uploads"

    canary_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True
    )
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    top_address: Mapped[str] = mapped_column(String(128), nullable=False)
    num_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    initial_verify_status: Mapped[ProdCanaryInitialVerifyStatus] = mapped_column(
        Enum(
            ProdCanaryInitialVerifyStatus,
            name="prodcanaryinitialverifystatus",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    storage_cost_atto: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(), nullable=True
    )
    gas_cost_wei: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(), nullable=True
    )
    chunk_attempts_total: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    deployment: Mapped["Deployment"] = relationship(  # noqa: F821
        back_populates="prod_canary_uploads"
    )
    chunks: Mapped[list["DeploymentProdCanaryChunk"]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
    )
    probes: Mapped[list["DeploymentProdCanaryProbe"]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
    )
    loss: Mapped[Optional["DeploymentProdCanaryLoss"]] = relationship(
        back_populates="upload",
        cascade="all, delete-orphan",
        uselist=False,
    )

    def to_dict(self):
        return {
            "canary_id": str(self.canary_id),
            "deployment_id": self.deployment_id,
            "source_url": self.source_url,
            "file_name": self.file_name,
            "file_size_bytes": self.file_size_bytes,
            "top_address": self.top_address,
            "num_chunks": self.num_chunks,
            "uploaded_at": self.uploaded_at.isoformat(),
            "initial_verify_status": self.initial_verify_status.value,
            "storage_cost_atto": (
                str(self.storage_cost_atto)
                if self.storage_cost_atto is not None
                else None
            ),
            "gas_cost_wei": (
                str(self.gas_cost_wei) if self.gas_cost_wei is not None else None
            ),
            "chunk_attempts_total": self.chunk_attempts_total,
            "recorded_at": self.recorded_at.isoformat(),
        }


class DeploymentProdCanaryChunk(Base):
    __tablename__ = "prod_canary_chunks"

    canary_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("prod_canary_uploads.canary_id", ondelete="CASCADE"),
        primary_key=True,
    )
    chunk_address: Mapped[str] = mapped_column(
        String(128), primary_key=True
    )
    chunk_role: Mapped[ProdCanaryChunkRole] = mapped_column(
        Enum(
            ProdCanaryChunkRole,
            name="prodcanarychunkrole",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )

    upload: Mapped["DeploymentProdCanaryUpload"] = relationship(
        back_populates="chunks"
    )

    def to_dict(self):
        return {
            "canary_id": str(self.canary_id),
            "chunk_address": self.chunk_address,
            "chunk_role": self.chunk_role.value,
        }


class DeploymentProdCanaryProbe(Base):
    __tablename__ = "prod_canary_probes"
    __table_args__ = (
        UniqueConstraint(
            "canary_id",
            "probed_at",
            "probed_by_host",
            name="uq_prod_canary_probes_group",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    canary_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("prod_canary_uploads.canary_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    probed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    probed_by_host: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ProdCanaryProbeStatus] = mapped_column(
        Enum(
            ProdCanaryProbeStatus,
            name="prodcanaryprobestatus",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    missing_chunk_address: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True
    )
    # Use JSONB on Postgres, JSON on other dialects (SQLite test fixtures).
    terminal_peer_outcomes: Mapped[Optional[dict]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    upload: Mapped["DeploymentProdCanaryUpload"] = relationship(
        back_populates="probes"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "canary_id": str(self.canary_id),
            "probed_at": self.probed_at.isoformat(),
            "probed_by_host": self.probed_by_host,
            "status": self.status.value,
            "missing_chunk_address": self.missing_chunk_address,
            "terminal_peer_outcomes": self.terminal_peer_outcomes,
            "duration_ms": self.duration_ms,
            "recorded_at": self.recorded_at.isoformat(),
        }


class DeploymentProdCanaryLoss(Base):
    __tablename__ = "prod_canary_loss"

    canary_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("prod_canary_uploads.canary_id", ondelete="CASCADE"),
        primary_key=True,
    )
    first_failed_probe_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    confirmed_lost_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    missing_chunk_address: Mapped[str] = mapped_column(
        String(128), nullable=False
    )
    hours_to_first_failure: Mapped[float] = mapped_column(Float, nullable=False)
    hours_to_confirmed_loss: Mapped[float] = mapped_column(Float, nullable=False)

    upload: Mapped["DeploymentProdCanaryUpload"] = relationship(
        back_populates="loss"
    )

    def to_dict(self):
        return {
            "canary_id": str(self.canary_id),
            "first_failed_probe_at": self.first_failed_probe_at.isoformat(),
            "confirmed_lost_at": self.confirmed_lost_at.isoformat(),
            "missing_chunk_address": self.missing_chunk_address,
            "hours_to_first_failure": self.hours_to_first_failure,
            "hours_to_confirmed_loss": self.hours_to_confirmed_loss,
        }
