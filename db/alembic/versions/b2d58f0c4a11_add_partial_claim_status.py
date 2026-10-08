"""add the PARTIAL claim status to the file pool

An upload that stored some chunks and then stopped is neither a success nor a
non-event. The file is not on the network in full, so it must go back in the
pool — completing it is useful work — but it is not untouched either: chunks
from it are in production right now.

Before this, ``import-history`` skipped ``partial_upload`` rows entirely and
``record-results`` left their pool claims open, reporting them as "no matching
upload result". Both presented a part-uploaded file as though nothing had ever
happened to it.

The partial unique index predicate widens from ``status <> 'released'`` to
``status NOT IN ('released', 'partial')``, so a PARTIAL claim does not block the
file from being drawn again. The index is dropped and recreated rather than
altered — Postgres has no ALTER INDEX for a predicate.

Revision ID: b2d58f0c4a11
Revises: a1c47e9b3d20
Create Date: 2026-07-31

"""

import sqlalchemy as sa
from alembic import op

revision = "b2d58f0c4a11"
down_revision = "a1c47e9b3d20"
branch_labels = None
depends_on = None

_INDEX = "uq_prod_file_pool_claims_blocking"
_TABLE = "prod_file_pool_claims"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
        # older servers; COMMIT first so this works regardless of server version.
        op.execute("COMMIT")
        op.execute("ALTER TYPE claimstatus ADD VALUE IF NOT EXISTS 'partial'")

    op.drop_index(_INDEX, table_name=_TABLE)
    op.create_index(
        _INDEX,
        _TABLE,
        ["file_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('released', 'partial')"),
        sqlite_where=sa.text("status NOT IN ('released', 'partial')"),
    )


def downgrade() -> None:
    # Any PARTIAL claims must go before the predicate narrows, or a file
    # carrying one would become permanently unclaimable under the old index.
    op.execute(
        f"UPDATE {_TABLE} SET status = 'released', "
        "note = coalesce(note || ' | ', '') || 'was partial before downgrade' "
        "WHERE status = 'partial'"
    )
    op.drop_index(_INDEX, table_name=_TABLE)
    op.create_index(
        _INDEX,
        _TABLE,
        ["file_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'released'"),
        sqlite_where=sa.text("status <> 'released'"),
    )
    # The enum value itself is left in place: Postgres cannot drop a value from
    # an enum type, and an unused value is harmless.
