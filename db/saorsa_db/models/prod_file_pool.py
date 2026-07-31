"""The pool of real files available for uploading to production Autonomi.

Production uploads must use real files, not generated junk, and a file must
never be uploaded twice — a canary measuring chunk loss on content that has
already been on the network for months is measuring nothing. This module holds
the catalogue of candidate files and the ledger of what has been handed out.

Three tables:

``prod_file_pool``
    One row per candidate file. Holds a *reference* — a URL or an absolute
    local path — never the bytes. The registry runs on DigitalOcean's smallest
    managed Postgres, where table size is query time.

``prod_file_pool_digests``
    Content digests, one row per (file, algorithm). A *set* rather than a
    single column because different sources publish different algorithms: the
    Internet Archive publishes MD5 and SHA1 per file in its item metadata
    (retrievable without downloading a byte), while a local file is whatever we
    choose to compute. Two entries can only be recognised as the same content
    if they share an algorithm, which is why local files are hashed with SHA1
    alongside SHA256.

``prod_file_pool_claims``
    One row per hand-out. A file is *available* when it has no open claim.
    This — rather than deriving availability from the upload results tables —
    is what lets the pool serve a GUI desktop application that has no link to
    this database at all: the checkout burns the file, not the upload record.

The operator-facing states of a file map onto claim status directly: available
(no blocking claim), claimed (``CLAIMED``, handed out but not confirmed on the
network) and uploaded (``UPLOADED``). Keeping ``CLAIMED`` distinct from
``UPLOADED`` matters in both directions — a forgotten desktop checkout must not
read as "on the network", and a released claim must not read as spent forever.

``PARTIAL`` is the fourth: an upload that stored some chunks and stopped. The
file goes back in the pool because completing it is useful work, but it is not
untouched — chunks from it are in production right now, which is why it is a
status of its own rather than a ``RELEASED``, and why canary selection excludes
it. A loss measurement over content that is already partly stored, possibly for
months, is measuring something other than what it claims to.
"""

import enum
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from saorsa_db.db import Base


class PoolFileKind(enum.Enum):
    """How a pooled file is obtained.

    The consumer chooses this, never the operator: a remote uploader VM can
    only fetch a URL, and a local upload can only read a path. There is
    deliberately no override, so no manifest can ask an uploader VM for a file
    on the operator's laptop.
    """

    URL = "url"
    LOCAL = "local"


class ClaimSource(enum.Enum):
    """What took the file.

    Closed and set by the tool, unlike ``ProdFilePoolClaim.label`` which is
    free text. The distinction is load-bearing: deployment-backed claims
    resolve themselves when ``deployments record-results`` runs, while manual
    ones can only ever be resolved by the operator.
    """

    PROD_UPLOADERS = "prod-uploaders"
    PROD_LOCAL_UPLOADS = "prod-local-uploads"
    PROD_CANARY = "prod-canary"
    MANUAL = "manual"


#: Sources whose claims are resolved automatically by ``record-results``.
#: A ``MANUAL`` claim never resolves on its own and needs the operator.
SELF_RESOLVING_SOURCES = frozenset(
    {ClaimSource.PROD_UPLOADERS, ClaimSource.PROD_CANARY}
)


class ClaimStatus(enum.Enum):
    CLAIMED = "claimed"
    UPLOADED = "uploaded"
    RELEASED = "released"
    #: The upload started and stored some chunks but did not finish. The file is
    #: NOT on the network in full, so it returns to the pool — completing it is
    #: useful work. Distinct from RELEASED, which means nothing was uploaded at
    #: all: a partial file has real chunks in production, and a view that showed
    #: it as untouched would be lying about what production holds.
    PARTIAL = "partial"


#: Statuses that make a file unavailable. A ``RELEASED`` or ``PARTIAL`` claim
#: returns the file to the pool; an ``UPLOADED`` one spends it permanently.
BLOCKING_STATUSES = frozenset({ClaimStatus.CLAIMED, ClaimStatus.UPLOADED})

#: Statuses that leave the file drawable again. Kept as a set rather than
#: inlined, because the partial unique index predicate must exclude exactly
#: these — if the two drift apart, the database and the availability query stop
#: agreeing about what "available" means.
NON_BLOCKING_STATUSES = frozenset({ClaimStatus.RELEASED, ClaimStatus.PARTIAL})


class ProdFilePool(Base):
    __tablename__ = "prod_file_pool"
    __table_args__ = (
        # Selection filters on kind + size within the active set, and the local
        # dataset alone runs to hundreds of thousands of rows, so this is the
        # index that decides whether `pool checkout` is instant or a table scan.
        Index("ix_prod_file_pool_selection", "kind", "active", "size_bytes"),
        Index("ix_prod_file_pool_duplicate_of", "duplicate_of"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[PoolFileKind] = mapped_column(
        Enum(PoolFileKind, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )

    # The URL or absolute local path. Text rather than a bounded String because
    # uniqueness is enforced on the hash below, not on this column: a btree
    # unique index over a long URL can exceed Postgres' per-entry size limit,
    # and a fixed 64-char index is far smaller across 300k+ rows besides.
    locator: Mapped[str] = mapped_column(Text, nullable=False)
    locator_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )

    name: Mapped[str] = mapped_column(String(512), nullable=False)

    # Nullable because a URL whose host publishes no Content-Length can still be
    # catalogued with --allow-unknown-size. Such an entry is invisible to
    # size-class selection, which is why the CLI flags it loudly rather than
    # letting it sit in the pool looking usable.
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Free-form labels for grouping (e.g. "video", "corpus"). Stored as JSON
    # rather than a side table: tag filtering is a convenience on a few thousand
    # curated URL entries, not part of the hot selection path. If it ever
    # becomes hot on the large local set, this wants a proper (tag, file) table.
    tags: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Where the entry came from, e.g. "internet-archive:<item>" or a dataset
    # name. Kept so a curated pool stays auditable long after the fact.
    provenance: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # Set when this entry was recognised as the same content as an existing
    # file — the Internet-Archive-URL-versus-local-copy case. Consuming either
    # row spends both, so the never-reuse guarantee survives the duplication.
    duplicate_of: Mapped[Optional[int]] = mapped_column(
        ForeignKey("prod_file_pool.id", ondelete="SET NULL"), nullable=True
    )

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    digests: Mapped[list["ProdFilePoolDigest"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )
    claims: Mapped[list["ProdFilePoolClaim"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "kind": self.kind.value,
            "locator": self.locator,
            "name": self.name,
            "size_bytes": self.size_bytes,
            "tags": self.tags or [],
            "provenance": self.provenance,
            "duplicate_of": self.duplicate_of,
            "active": self.active,
            "notes": self.notes,
            "added_at": self.added_at.isoformat(),
        }


class ProdFilePoolDigest(Base):
    __tablename__ = "prod_file_pool_digests"
    __table_args__ = (
        UniqueConstraint("file_id", "algo", name="uq_prod_file_pool_digests_file_algo"),
        # The lookup that makes cross-kind duplicate detection possible: given a
        # digest an Internet Archive manifest just handed us, does any existing
        # file already carry it?
        Index("ix_prod_file_pool_digests_lookup", "algo", "value"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("prod_file_pool.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "md5", "sha1", "sha256". Lower-case by convention; the repository
    # normalises on write so a lookup never misses on case alone.
    algo: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(128), nullable=False)

    file: Mapped["ProdFilePool"] = relationship(back_populates="digests")

    def to_dict(self):
        return {"algo": self.algo, "value": self.value}


class ProdFilePoolClaim(Base):
    __tablename__ = "prod_file_pool_claims"
    __table_args__ = (
        # At most one BLOCKING claim per file — the invariant the whole design
        # rests on. A partial unique index rather than an application check
        # because two concurrent deploys racing for the last large file is
        # exactly the case that must not both succeed.
        #
        # The predicate excludes the non-blocking statuses rather than matching
        # only CLAIMED, so it covers the stronger rule: a file that has been
        # UPLOADED can never be claimed again either. RELEASED and PARTIAL do
        # not block — a file handed back, or only part-uploaded, can be handed
        # out again, and may accumulate several such claims over its life.
        #
        # Partial indexes work on both Postgres and the SQLite used in tests, so
        # this guarantee is exercised by the suite rather than only asserted.
        Index(
            "uq_prod_file_pool_claims_blocking",
            "file_id",
            unique=True,
            postgresql_where=text("status NOT IN ('released', 'partial')"),
            sqlite_where=text("status NOT IN ('released', 'partial')"),
        ),
        Index("ix_prod_file_pool_claims_status", "status"),
        Index("ix_prod_file_pool_claims_deployment", "deployment_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("prod_file_pool.id", ondelete="CASCADE"), nullable=False, index=True
    )

    source: Mapped[ClaimSource] = mapped_column(
        Enum(ClaimSource, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # Free text, and only meaningful for MANUAL claims: "desktop-app",
    # "phone-testing". Open-ended by nature — an enum would need a tool release
    # for every new client the operator wants to feed by hand.
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # SET NULL, not CASCADE: if the deployment row goes, the file is still on
    # the network and the claim must survive to say so. The name is snapshotted
    # alongside because deployment names are reused constantly, so resolving the
    # id later would attribute the upload to the wrong run.
    deployment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("deployments.id", ondelete="SET NULL"), nullable=True
    )
    deployment_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    status: Mapped[ClaimStatus] = mapped_column(
        Enum(ClaimStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ClaimStatus.CLAIMED,
    )

    # Populated on resolution. The datamap address is a content identity —
    # measured on this registry, every file uploaded more than once produced
    # exactly one address — so it doubles as the post-hoc duplicate check for
    # entries no digest could match beforehand.
    address: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    chunks_stored: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chunks_failed: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_chunks: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # NUMERIC, never float: atto and wei are uint256-shaped and a float would
    # silently lose precision. Mirrors deployment_prod_upload_results.
    storage_cost_atto: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(), nullable=True
    )
    gas_cost_wei: Mapped[Optional[Decimal]] = mapped_column(Numeric(), nullable=True)

    # Which locator was actually used. A claim attaches to the *canonical*
    # file, but that content may be reachable two ways — a local copy and an
    # Internet Archive URL — and knowing which one a run fetched is the
    # difference between a reproducible audit and a guess.
    locator_used: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    file: Mapped["ProdFilePool"] = relationship(back_populates="claims")

    @property
    def is_open(self) -> bool:
        return self.status is ClaimStatus.CLAIMED

    @property
    def blocks_selection(self) -> bool:
        """Whether this claim stops the file being handed out again."""
        return self.status in BLOCKING_STATUSES

    @property
    def self_resolving(self) -> bool:
        """Whether ``record-results`` will resolve this claim without the operator.

        Drives the distinction the ``claims`` view makes: nagging about a
        deployment-backed claim that is merely awaiting a recording run trains
        the operator to ignore the list that also carries the real ones.
        """
        return self.source in SELF_RESOLVING_SOURCES

    def to_dict(self):
        return {
            "id": self.id,
            "file_id": self.file_id,
            "source": self.source.value,
            "label": self.label,
            "deployment_id": self.deployment_id,
            "deployment_name": self.deployment_name,
            "status": self.status.value,
            "address": self.address,
            "chunks_stored": self.chunks_stored,
            "chunks_failed": self.chunks_failed,
            "total_chunks": self.total_chunks,
            "storage_cost_atto": (
                str(self.storage_cost_atto)
                if self.storage_cost_atto is not None
                else None
            ),
            "gas_cost_wei": (
                str(self.gas_cost_wei) if self.gas_cost_wei is not None else None
            ),
            "locator_used": self.locator_used,
            "note": self.note,
            "claimed_at": self.claimed_at.isoformat(),
            "resolved_at": (
                self.resolved_at.isoformat() if self.resolved_at is not None else None
            ),
        }
