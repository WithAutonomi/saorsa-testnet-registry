"""add client_kind and the two pre-payment statuses to prod upload results

A `prod-uploaders` deployment can now run a native uploader and a WASM
(browser-path) uploader side by side, and the two are not the same instrument —
the browser path measured several times slower, and it refuses a file above
1,000,000,000 bytes outright. Both tiers write the same `prod_upload`
measurement, so without `client_kind` every query blends them, which is the
opposite of what running both is for. NULL on every existing row: they all
predate the browser path and were therefore native. Deliberately not backfilled,
so a row's provenance stays distinguishable from an assumption made here.

Two new statuses, both of which only the browser path can report:

``failed`` — the upload failed BEFORE any payment transaction went out, so
nothing reached the network. Distinct from ``partial_upload``, which means chunks
ARE stored: the pool claim for a ``failed`` file is released (the file is
untouched, and a canary may still draw it) where a partial one is spent. The
native uploader cannot tell the two apart and so never records this.

``over_cap`` — the file is larger than the transport will carry. Nothing was
attempted and nothing was paid for, so it is neither a success nor a failure of
the network.

Revision ID: e7c4b02d9a13
Revises: d3f7a91c60be
Create Date: 2026-09-26

"""

import sqlalchemy as sa
from alembic import op

revision = "e7c4b02d9a13"
down_revision = "d3f7a91c60be"
branch_labels = None
depends_on = None

_TABLE = "deployment_prod_upload_results"
_STATUS_ENUM_NAME = "produploadstatus"
_NEW_VALUES = ("failed", "over_cap")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
        # older servers; COMMIT first so this works regardless of server version.
        op.execute("COMMIT")
        for value in _NEW_VALUES:
            op.execute(
                f"ALTER TYPE {_STATUS_ENUM_NAME} ADD VALUE IF NOT EXISTS '{value}'"
            )

    op.add_column(_TABLE, sa.Column("client_kind", sa.String(length=16), nullable=True))


def downgrade() -> None:
    # Rows carrying a status the application no longer knows must go before the
    # column does. They are deleted rather than relabelled: calling a pre-payment
    # failure a partial upload would mark a pool file spent when nothing was ever
    # stored, which is the exact misreading these statuses exist to prevent.
    values = ", ".join(f"'{value}'" for value in _NEW_VALUES)
    op.execute(f"DELETE FROM {_TABLE} WHERE status IN ({values})")
    op.drop_column(_TABLE, "client_kind")
    # The enum values themselves are left in place: Postgres cannot drop a value
    # from an enum type, and an unused value is harmless.
