"""Independent read-back verification of a production upload (V2-837).

The daily write test uploads a file to production and then has other regions
download it again and check that what came back is byte-for-byte what went up.
This table is where those checks land — one row per (deployment, verifying
host, address).

Two things make this table the authority rather than InfluxDB, which also
receives the same measurements for dashboards:

* **Retention.** A deployment's Influx bucket expires points (7 days by
  default). A verifier VM that is offline for longer would otherwise miss
  those addresses permanently, with nothing to notice the gap.
* **It doubles as the work queue.** "Which addresses has this host not checked
  yet?" is a LEFT JOIN against this table, so a verifier keeps no local state
  and a replaced VM picks up exactly where its predecessor left off.

`DIGEST_MISMATCH` is deliberately its own status rather than a flavour of
failure. A download that fails is a availability problem; a download that
succeeds and returns different bytes is a correctness problem, and the two
should never be summed.

**Only `OK` and `DIGEST_MISMATCH` are terminal.** A `DOWNLOAD_FAILED` or
`INCONCLUSIVE` row is re-queued on the next cycle, because otherwise a single
transient blip would mark an address failed forever and never re-check it —
reporting a healthy file as unavailable, in the one direction that matters.
Since a retry overwrites the row, `attempts` and `first_failed_at` carry what
would otherwise be lost: whether this is a blip or a week-long outage.
"""

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class ProdUploadVerifyStatus(enum.Enum):
    # Downloaded and the hash matched what the uploader recorded.
    OK = "ok"
    # Downloaded, but the bytes differ from what was uploaded. The alarming
    # one: the network returned something, and it was wrong.
    DIGEST_MISMATCH = "digest_mismatch"
    # `ant` could not retrieve the address at all.
    DOWNLOAD_FAILED = "download_failed"
    # Something prevented a verdict that says nothing about the network — a
    # timeout, the verifier being killed mid-download, no expected hash on the
    # upload row. Kept distinct so it can never be counted as a loss, mirroring
    # the canary's `inconclusive`.
    INCONCLUSIVE = "inconclusive"


#: Verdicts that answer the question for good. Everything else is retried on
#: the next cycle — see the module docstring.
TERMINAL_VERIFY_STATUSES = frozenset(
    {ProdUploadVerifyStatus.OK, ProdUploadVerifyStatus.DIGEST_MISMATCH}
)


class DeploymentProdUploadVerification(Base):
    __tablename__ = "deployment_prod_upload_verifications"
    __table_args__ = (
        # One verdict per (deployment, verifying host, address). The verifiers
        # deliberately do not shard — each region checks every address — so the
        # hostname is part of the key rather than the address alone.
        UniqueConstraint(
            "deployment_id",
            "client_hostname",
            "address",
            name="uq_deployment_prod_upload_verifications_group",
        ),
        # The verifier's work-queue lookup: everything this deployment has
        # verified, narrowed by host.
        Index(
            "ix_prod_upload_verifications_queue",
            "deployment_id",
            "client_hostname",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The verifying host — NOT the host that uploaded.
    client_hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    address: Mapped[str] = mapped_column(String(128), nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)

    status: Mapped[ProdUploadVerifyStatus] = mapped_column(
        Enum(ProdUploadVerifyStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # Both hashes are kept, not just a boolean, so a mismatch is evidence
    # rather than an assertion.
    expected_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    actual_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # The ant-client commit the WASM bundle that performed this check was built
    # from. The independent verifier VMs are NATIVE and so always write NULL;
    # this is populated by the uploader VM's own read-back, which runs over
    # whichever transport the upload used.
    client_build: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    bytes_downloaded: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # How many times this host has checked this address. A retry overwrites the
    # row, so without this a file that has failed every hour for a week is
    # indistinguishable from one that failed once — and that difference is the
    # whole signal.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # When it first went wrong, kept even after a later recovery: "was fine,
    # broke on Tuesday, recovered Thursday" is worth more than "fine now".
    first_failed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # How long after the upload finished this check ran. Denormalised at write
    # time because the upload row can be pruned or the deployment reused, and
    # "confirmed again later" is the whole point of the second region.
    age_at_check_secs: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

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
        back_populates="prod_upload_verifications"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "deployment_id": self.deployment_id,
            "client_hostname": self.client_hostname,
            "region": self.region,
            "provider": self.provider,
            "address": self.address,
            "file_name": self.file_name,
            "status": self.status.value,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "client_build": self.client_build,
            "bytes_downloaded": self.bytes_downloaded,
            "duration_ms": self.duration_ms,
            "reason": self.reason,
            "attempts": self.attempts,
            "first_failed_at": (
                self.first_failed_at.isoformat() if self.first_failed_at else None
            ),
            "age_at_check_secs": self.age_at_check_secs,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
        }
