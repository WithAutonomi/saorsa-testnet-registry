"""Channel state per release: what shipped to beta, what was promoted to
stable, and what the beta channel rolled past without promoting.

Deliberately a table of its own rather than lifecycle columns on
:class:`~saorsa_db.models.release_candidate.ReleaseCandidate`: an ``rc.N``
respin cuts a *new* RC row, so a train that needed three respins has three RC
rows but only ever ships one release. The release is the lineage that shipped;
the RC is one attempt at cutting it.

Granularity matches the RC tables — **one row per binary target**. A train
records two releases (``ant-node`` and ``ant``) sharing one ``rc_branch``, and
some trains ship only one.
"""

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class ReleaseOutcome(enum.Enum):
    """Where a release got to on the channels.

    ``SHIPPED_TO_BETA`` is the only entry state — nothing is recorded here
    until Wednesday's beta ship. The other two are **terminal**: a promoted
    release is on stable and a rolled-past one never will be, so neither is
    reopened. The fixes in a rolled-past train ride the next one.
    """

    SHIPPED_TO_BETA = "shipped_to_beta"
    PROMOTED = "promoted"
    ROLLED_PAST = "rolled_past"


TERMINAL_RELEASE_OUTCOMES = (
    ReleaseOutcome.PROMOTED,
    ReleaseOutcome.ROLLED_PAST,
)


class Release(Base):
    """One binary target's release lineage through the beta and stable channels."""

    __tablename__ = "releases"
    __table_args__ = (
        UniqueConstraint(
            "binary_target",
            "version",
            name="uq_releases_binary_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    binary_target: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    #: The final, suffix-free version (``0.17.1``) — NOT ``0.17.1-beta.1``.
    #: The beta and final tags carry the suffixed forms.
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    rc_branch: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    #: The RC row this release shipped from. Nullable, and resolved by the
    #: ``(binary_target, binary_version)`` natural key rather than by id —
    #: ``release-from-rc`` never holds the RC row id, and a ship must still be
    #: recordable when the RC row is missing.
    release_candidate_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("release_candidates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    beta_tag: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    beta_tag_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    shipped_to_beta_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The soak deadline — the Monday the ship record planned to promote on.
    planned_promotion_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    final_tag: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    final_tag_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    promoted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    outcome: Mapped[ReleaseOutcome] = mapped_column(
        Enum(
            ReleaseOutcome,
            name="releaseoutcome",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        index=True,
    )
    #: Free text. A rolled-past release records *why* here — that reason is the
    #: whole value of the row.
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    crates: Mapped[list["ReleaseCrate"]] = relationship(
        back_populates="release",
        order_by="ReleaseCrate.id",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "binary_target": self.binary_target,
            "version": self.version,
            "rc_branch": self.rc_branch,
            "release_candidate_id": self.release_candidate_id,
            "beta_tag": self.beta_tag,
            "beta_tag_sha": self.beta_tag_sha,
            "shipped_to_beta_at": (
                self.shipped_to_beta_at.isoformat()
                if self.shipped_to_beta_at
                else None
            ),
            "planned_promotion_at": (
                self.planned_promotion_at.isoformat()
                if self.planned_promotion_at
                else None
            ),
            "final_tag": self.final_tag,
            "final_tag_sha": self.final_tag_sha,
            "promoted_at": (
                self.promoted_at.isoformat() if self.promoted_at else None
            ),
            "outcome": self.outcome.value,
            "notes": self.notes,
            "recorded_at": self.recorded_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "crates": [c.to_dict() for c in self.crates],
        }


class ReleaseCrate(Base):
    """One crate as it was actually released — the release manifest.

    Not the same rows as :class:`ReleaseCandidateCrate`: those carry the
    rc-suffixed versions and the RC tags (``evmlib 0.9.1-rc.1``,
    ``ant-cli-v0.3.3-rc.1``), whereas a released crate carries the suffix-free
    version and the final tag. Reusing the RC rows would record the wrong
    versions against the release.
    """

    __tablename__ = "release_crates"
    __table_args__ = (
        UniqueConstraint(
            "release_id",
            "crate_name",
            name="uq_release_crates_release_crate",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("releases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    crate_name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    repo_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Null for a crate that is bumped but never tagged — ``ant-core`` is
    #: bumped with every ant-client release and tagged with none of them.
    tag: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tag_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    release: Mapped["Release"] = relationship(back_populates="crates")

    def to_dict(self):
        return {
            "id": self.id,
            "release_id": self.release_id,
            "crate_name": self.crate_name,
            "version": self.version,
            "repo_slug": self.repo_slug,
            "tag": self.tag,
            "tag_sha": self.tag_sha,
        }
