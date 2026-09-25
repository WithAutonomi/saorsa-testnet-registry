"""add the download-record address column and the SKIPPED op status

Two gaps the production WASM (browser-path) download kinds opened up, both of
which apply to the native path as well.

``address``: the ``download`` Influx measurement carried no address, so a
recorded operation said that something failed but never what it had tried to
fetch. With a 6-address list that is guessable; with the production list it is
not, and nothing else maps an operation to its target. Both transports now emit
the field, and it is nullable because no past row can be backfilled — the Influx
points those rows came from never held it.

``skipped``: ant-core refuses a browser-path file above 1,000,000,000 bytes.
That is a property of the transport, not of the network — the content is stored
and healthy — so counting it as a download failure would report a transport
limitation as a network fault. The row is kept (a skip is a real event worth
seeing) but excluded from every success-rate denominator.

Revision ID: d3f7a91c60be
Revises: b8f2a1c5d740
Create Date: 2026-09-25

"""

import sqlalchemy as sa
from alembic import op

revision = "d3f7a91c60be"
down_revision = "b8f2a1c5d740"
branch_labels = None
depends_on = None

_TABLE = "deployment_download_records"
_STATUS_ENUM_NAME = "oprecordstatus"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
        # older servers; COMMIT first so this works regardless of server version.
        op.execute("COMMIT")
        op.execute(f"ALTER TYPE {_STATUS_ENUM_NAME} ADD VALUE IF NOT EXISTS 'skipped'")

    op.add_column(_TABLE, sa.Column("address", sa.String(length=255), nullable=True))


def downgrade() -> None:
    # Skipped rows must go before the value disappears from the application's
    # vocabulary. They are deleted rather than relabelled: calling one a failure
    # is exactly the misreading the status exists to prevent, and calling one a
    # success is worse.
    op.execute(f"DELETE FROM {_TABLE} WHERE status = 'skipped'")
    op.execute("DELETE FROM deployment_upload_records WHERE status = 'skipped'")
    op.drop_column(_TABLE, "address")
    # The enum value itself is left in place: Postgres cannot drop a value from
    # an enum type, and an unused value is harmless.
