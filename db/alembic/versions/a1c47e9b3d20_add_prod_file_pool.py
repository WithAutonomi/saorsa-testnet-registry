"""add the production upload file pool

Three tables backing a managed pool of real files for production uploads:
``prod_file_pool`` (the catalogue — references, never bytes),
``prod_file_pool_digests`` (content digests, one row per file+algorithm, so a
local SHA1 and an Internet Archive SHA1 can be recognised as the same content)
and ``prod_file_pool_claims`` (the hand-out ledger — a file is available when
it has no open claim).

Two indexes here are load-bearing rather than incidental:

* ``ix_prod_file_pool_selection`` over (kind, active, size_bytes). The local
  side of the pool is expected to hold hundreds of thousands of rows, and
  selection always filters kind + active + a size-class byte range.
* ``uq_prod_file_pool_claims_open``, a PARTIAL unique index over file_id where
  status = 'claimed'. This is what makes "at most one open claim per file" a
  database guarantee rather than a hope: two concurrent deploys racing for the
  last large file must not both win. Partial indexes are supported by both
  Postgres and the SQLite used in the test suite, so the constraint is
  exercised by tests rather than only asserted in production.

Uniqueness of ``locator`` is enforced through ``locator_sha256`` rather than
the column itself — a btree unique index over a long URL can exceed Postgres'
per-entry size limit, and a fixed 64-char index is materially smaller across
300k+ rows.

Revision ID: a1c47e9b3d20
Revises: e4a7b2c91d05
Create Date: 2026-07-30

"""

import sqlalchemy as sa
from alembic import op

revision = "a1c47e9b3d20"
down_revision = "e4a7b2c91d05"
branch_labels = None
depends_on = None

_POOL_FILE_KIND = sa.Enum("url", "local", name="poolfilekind")
_CLAIM_SOURCE = sa.Enum(
    "prod-uploaders",
    "prod-local-uploads",
    "prod-canary",
    "manual",
    name="claimsource",
)
_CLAIM_STATUS = sa.Enum("claimed", "uploaded", "released", name="claimstatus")


def upgrade() -> None:
    op.create_table(
        "prod_file_pool",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", _POOL_FILE_KIND, nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("locator_sha256", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        # Nullable: a URL whose host publishes no Content-Length can still be
        # catalogued, it is simply invisible to size-class selection.
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("provenance", sa.String(length=512), nullable=True),
        sa.Column("duplicate_of", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["duplicate_of"], ["prod_file_pool.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_prod_file_pool_locator_sha256",
        "prod_file_pool",
        ["locator_sha256"],
        unique=True,
    )
    op.create_index(
        "ix_prod_file_pool_selection",
        "prod_file_pool",
        ["kind", "active", "size_bytes"],
    )
    op.create_index(
        "ix_prod_file_pool_duplicate_of", "prod_file_pool", ["duplicate_of"]
    )

    op.create_table(
        "prod_file_pool_digests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("algo", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["prod_file_pool.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "file_id", "algo", name="uq_prod_file_pool_digests_file_algo"
        ),
    )
    op.create_index(
        "ix_prod_file_pool_digests_file_id", "prod_file_pool_digests", ["file_id"]
    )
    op.create_index(
        "ix_prod_file_pool_digests_lookup",
        "prod_file_pool_digests",
        ["algo", "value"],
    )

    op.create_table(
        "prod_file_pool_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("source", _CLAIM_SOURCE, nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("deployment_id", sa.Integer(), nullable=True),
        sa.Column("deployment_name", sa.String(length=128), nullable=True),
        sa.Column("status", _CLAIM_STATUS, nullable=False),
        sa.Column("address", sa.String(length=128), nullable=True),
        sa.Column("chunks_stored", sa.Integer(), nullable=True),
        sa.Column("chunks_failed", sa.Integer(), nullable=True),
        sa.Column("total_chunks", sa.Integer(), nullable=True),
        sa.Column("storage_cost_atto", sa.Numeric(), nullable=True),
        sa.Column("gas_cost_wei", sa.Numeric(), nullable=True),
        # Which locator the run actually fetched. A claim attaches to canonical
        # content that may be reachable both as a local path and as an Internet
        # Archive URL; this records which was used.
        sa.Column("locator_used", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["prod_file_pool.id"], ondelete="CASCADE"),
        # SET NULL, not CASCADE: deleting a deployment row must not delete the
        # evidence that its files were uploaded and are therefore spent.
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployments.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_prod_file_pool_claims_file_id", "prod_file_pool_claims", ["file_id"]
    )
    op.create_index(
        "ix_prod_file_pool_claims_status", "prod_file_pool_claims", ["status"]
    )
    op.create_index(
        "ix_prod_file_pool_claims_deployment",
        "prod_file_pool_claims",
        ["deployment_id"],
    )
    # Excludes RELEASED rather than matching only CLAIMED: an UPLOADED file must
    # never be claimed a second time either, while a released claim frees the
    # file and several may accumulate over its life.
    op.create_index(
        "uq_prod_file_pool_claims_blocking",
        "prod_file_pool_claims",
        ["file_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'released'"),
        sqlite_where=sa.text("status <> 'released'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_prod_file_pool_claims_blocking", table_name="prod_file_pool_claims"
    )
    op.drop_index(
        "ix_prod_file_pool_claims_deployment", table_name="prod_file_pool_claims"
    )
    op.drop_index("ix_prod_file_pool_claims_status", table_name="prod_file_pool_claims")
    op.drop_index(
        "ix_prod_file_pool_claims_file_id", table_name="prod_file_pool_claims"
    )
    op.drop_table("prod_file_pool_claims")

    op.drop_index(
        "ix_prod_file_pool_digests_lookup", table_name="prod_file_pool_digests"
    )
    op.drop_index(
        "ix_prod_file_pool_digests_file_id", table_name="prod_file_pool_digests"
    )
    op.drop_table("prod_file_pool_digests")

    op.drop_index("ix_prod_file_pool_duplicate_of", table_name="prod_file_pool")
    op.drop_index("ix_prod_file_pool_selection", table_name="prod_file_pool")
    op.drop_index("ix_prod_file_pool_locator_sha256", table_name="prod_file_pool")
    op.drop_table("prod_file_pool")

    bind = op.get_bind()
    _CLAIM_STATUS.drop(bind, checkfirst=True)
    _CLAIM_SOURCE.drop(bind, checkfirst=True)
    _POOL_FILE_KIND.drop(bind, checkfirst=True)
