"""add content verification to the testnet transfer records

The data-integrity hard gate was NOT ASSESSED on every staging verdict because
nothing anywhere verified the content of a downloaded file: a transfer that
"succeeded" while silently returning corrupt bytes passed every check the
system made (V2-757).

The uploader now publishes the SHA-256 of each generated file on its address
line and emits it on the `upload` Influx point; the downloader hashes what it
got back and compares. These columns are where that evidence lands.

``verify_status`` is NULL for "not applicable", never for a failure: the
download failed and had no bytes to check, or the row predates verification.
``expected_sha256``/``actual_sha256`` are populated on a MISMATCH only — on a
match they are equal and the uploader's hash is already recorded on the upload
row, and on an unverifiable download there is nothing to record.

Nullable with no default and no backfill: the Influx points behind every
existing row never carried these fields, so no past run can be corrected.

Revision ID: c7a4d15e83b2
Revises: b8e2f4a61c37
Create Date: 2026-09-29

"""

import sqlalchemy as sa
from alembic import op

revision = "c7a4d15e83b2"
down_revision = "b8e2f4a61c37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deployment_upload_records",
        sa.Column("sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "deployment_download_records",
        sa.Column("verify_status", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "deployment_download_records",
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "deployment_download_records",
        sa.Column("actual_sha256", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deployment_download_records", "actual_sha256")
    op.drop_column("deployment_download_records", "expected_sha256")
    op.drop_column("deployment_download_records", "verify_status")
    op.drop_column("deployment_upload_records", "sha256")
