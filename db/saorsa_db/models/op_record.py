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
    # The operation was deliberately not attempted, or was refused by the
    # transport rather than by the network: a WASM (browser-path) client cannot
    # carry a file above ant-core's 1 GB cap, so an over-cap entry in the address
    # list is skipped. NOT a failure — the content is on the network and healthy
    # — so every success-rate denominator excludes these rows. Counting them as
    # failures would report a transport limitation as a network fault.
    SKIPPED = "skipped"


class _BaseOpRecord:
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
    # Which client transport performed this operation: "native" (the `ant`
    # binary over QUIC) or "wasm" (ant-core compiled to WASM over
    # WebRTC-Direct). NULL on every row predating the WASM client, which was
    # necessarily native.
    #
    # Denormalised onto each op for the same reason as network_profile: the
    # whole point of running both transports on one testnet is to compare them,
    # and a comparison that has to join through deploy_args JSON to tell them
    # apart is a comparison nobody runs. Both transports emit the same
    # `upload`/`download` Influx measurements, so this column — resolved from
    # the client VM's role — is what keeps them separable.
    client_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
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
            "network_profile": self.network_profile,
            "client_kind": self.client_kind,
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

    # The public address this operation fetched. NULL on every row predating the
    # column, because the `download` Influx measurement carried no address at
    # all: an op could be seen to have failed but not what it had tried to
    # fetch, so a per-address failure was uninvestigable. Downloads only — an
    # upload's address is its RESULT and lives in the prod-upload tables.
    address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    deployment: Mapped["Deployment"] = relationship(  # noqa: F821
        back_populates="download_records"
    )

    def to_dict(self):
        return {**super().to_dict(), "address": self.address}
