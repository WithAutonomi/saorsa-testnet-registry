"""add error_kind and reason to every transfer record table

A failure in the four permanent production transfer deployments was not
diagnosable after the fact, and that was one problem with one cause: nobody
wrote down enough at the time. The durable copy of a failed download was
`status=failure`, `file_size_bytes=0` and a duration -- PROD-WDL-01 had eleven
such rows and not one said anything about why (INFRA-484).

Two fields, because they answer two different questions. `reason` is `ant`'s own
sentence, clipped to 500 characters with hex blobs elided, which is what an
operator reads. `error_kind` is its classification from one closed vocabulary
(`saorsa_db.models.transfer_error_kind.TransferErrorKind`), which is what a
failure count can be grouped by.

`String(32)` and not a Postgres enum type, deliberately, exactly as
`deployment_browser_download_records.error_kind` already is: a kind added to a
classifier has to be recordable without a migration standing between it and a
run.

`deployment_prod_upload_verifications` is deliberately excluded. Its `reason` is
composed entirely by us and its four-value status already IS the kind, so an
`error_kind` there would be a second spelling of one fact.

NOTHING IS BACKFILLED and no past row is rewritten. The Influx points the
existing rows came from never carried either field, so NULL means "not
recorded" and never "no error". Whatever is still inside a deployment's
retention window is picked up by the next `deployments archive` at no cost,
because that pass re-upserts the same rows and the column list is the only
thing that changed.

Revision ID: a2e71c4b9f36
Revises: d1a6f03b7c58
Create Date: 2026-11-03

"""

import sqlalchemy as sa
from alembic import op

revision = "a2e71c4b9f36"
down_revision = "d1a6f03b7c58"
branch_labels = None
depends_on = None

#: The per-operation record tables, which share `_BaseOpRecord` and so gain
#: both fields: neither carried either of them.
_OP_RECORD_TABLES = (
    "deployment_upload_records",
    "deployment_download_records",
)

#: The prod result tables, which already carry `reason` and gain only the kind.
_PROD_RESULT_TABLES = (
    "deployment_prod_upload_results",
    "deployment_prod_download_results",
)

# No `comment=` on any of these: nothing else in this schema carries a database
# comment, so one here would be a permanent `--autogenerate` diff against the
# models. What NULL means is recorded where it is read -- beside each column in
# `saorsa_db/models/`.


def upgrade() -> None:
    for table in _OP_RECORD_TABLES:
        op.add_column(table, sa.Column("error_kind", sa.String(32), nullable=True))
        op.add_column(table, sa.Column("reason", sa.Text(), nullable=True))
    for table in _PROD_RESULT_TABLES:
        op.add_column(table, sa.Column("error_kind", sa.String(32), nullable=True))


def downgrade() -> None:
    for table in _PROD_RESULT_TABLES:
        op.drop_column(table, "error_kind")
    for table in _OP_RECORD_TABLES:
        op.drop_column(table, "reason")
        op.drop_column(table, "error_kind")
