"""add client_kind to deployment upload/download op records

Distinguishes the client transport that performed each file operation: the
native `ant` binary over QUIC, or ant-core compiled to WASM over WebRTC-Direct.
Both emit the same `upload`/`download` InfluxDB measurements, so without this
column a testnet running both transports blends them in every query -- which is
the opposite of what running both is for.

NULL on every existing row: they all predate the WASM client and were therefore
native. Deliberately not backfilled to "native", so a row's provenance stays
distinguishable from an assumption made by this migration.

Revision ID: c3d81f0a95b2
Revises: a7e4c93b1f28
"""

import sqlalchemy as sa
from alembic import op

revision = "c3d81f0a95b2"
down_revision = "a7e4c93b1f28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("deployment_upload_records", "deployment_download_records"):
        op.add_column(table, sa.Column("client_kind", sa.String(16), nullable=True))


def downgrade() -> None:
    for table in ("deployment_upload_records", "deployment_download_records"):
        op.drop_column(table, "client_kind")
