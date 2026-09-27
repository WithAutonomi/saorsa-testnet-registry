"""add client_build to the transfer result tables

Records which build of the WASM (browser-path) client performed each transfer,
as `ant-wasm` itself reported it from the `build-info.json` in its bundle
(V2-1346).

The deployment already carries ``deploy_args.wasm_build``, but that holds only
the CURRENT build and is overwritten — and `deployments upgrade-wasm-client`
exists precisely to replace the bundle underneath a permanently-running
deployment. Without a per-row stamp the transfers either side of such an upgrade
are indistinguishable, which is the whole point of doing the upgrade in place.

Nullable with no default and no backfill, and NULL means "unknown build": it is
what a native row carries (the `ant` binary is recorded per deployment, not per
op), what a native verifier VM carries, and what a browser-path row whose bundle
predates build provenance carries. No past row can be corrected — the Influx
points never held the field.

Revision ID: b8e2f4a61c37
Revises: 233c7d287e11
Create Date: 2026-09-27

"""

import sqlalchemy as sa
from alembic import op

revision = "b8e2f4a61c37"
down_revision = "233c7d287e11"
branch_labels = None
depends_on = None

_TABLES = (
    "deployment_download_records",
    "deployment_prod_upload_results",
    "deployment_prod_upload_verifications",
)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("client_build", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "client_build")
