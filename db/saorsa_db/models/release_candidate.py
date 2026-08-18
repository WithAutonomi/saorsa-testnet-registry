from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class ReleaseCandidate(Base):
    """One row per actual release candidate (e.g. ``ant-node 0.11.1-rc.1``).

    Each RC owns a set of :class:`ReleaseCandidateCrate` rows — one per
    crate that was bumped + branched as part of cutting this RC.
    """

    __tablename__ = "release_candidates"
    __table_args__ = (
        UniqueConstraint(
            "binary_target",
            "binary_version",
            name="uq_release_candidates_binary_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    binary_target: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    binary_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rc_branch: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    crates: Mapped[list["ReleaseCandidateCrate"]] = relationship(
        back_populates="release_candidate",
        order_by="ReleaseCandidateCrate.id",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "binary_target": self.binary_target,
            "binary_version": self.binary_version,
            "rc_branch": self.rc_branch,
            "notes": self.notes,
            "recorded_at": self.recorded_at.isoformat(),
            "crates": [c.to_dict() for c in self.crates],
        }


class ReleaseCandidateCrate(Base):
    """One crate bumped + branched as part of a release candidate."""

    __tablename__ = "release_candidate_crates"
    __table_args__ = (
        UniqueConstraint(
            "release_candidate_id",
            "crate_name",
            name="uq_release_candidate_crates_rc_crate",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    release_candidate_id: Mapped[int] = mapped_column(
        ForeignKey("release_candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    crate_name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    repo_slug: Mapped[str] = mapped_column(String(128), nullable=False)
    tag: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tag_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    since_tag: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    since_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    head_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    release_candidate: Mapped["ReleaseCandidate"] = relationship(
        back_populates="crates"
    )
    prs: Mapped[list["ReleaseCandidatePR"]] = relationship(
        back_populates="crate",
        order_by="ReleaseCandidatePR.merged_at",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "release_candidate_id": self.release_candidate_id,
            "crate_name": self.crate_name,
            "version": self.version,
            "repo_slug": self.repo_slug,
            "tag": self.tag,
            "tag_sha": self.tag_sha,
            "since_tag": self.since_tag,
            "since_sha": self.since_sha,
            "head_sha": self.head_sha,
            "prs": [pr.to_dict() for pr in self.prs],
        }


class ReleaseCandidatePR(Base):
    """A pull request that landed in a given RC's crate."""

    __tablename__ = "release_candidate_prs"
    __table_args__ = (
        UniqueConstraint(
            "release_candidate_crate_id",
            "pr_number",
            name="uq_release_candidate_prs_crate_pr",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    release_candidate_crate_id: Mapped[int] = mapped_column(
        ForeignKey("release_candidate_crates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pr_number: Mapped[int] = mapped_column(nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    author: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    merged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    crate: Mapped["ReleaseCandidateCrate"] = relationship(back_populates="prs")

    def to_dict(self):
        return {
            "id": self.id,
            "release_candidate_crate_id": self.release_candidate_crate_id,
            "pr_number": self.pr_number,
            "title": self.title,
            "author": self.author,
            "merged_at": self.merged_at.isoformat() if self.merged_at else None,
            "url": self.url,
        }
