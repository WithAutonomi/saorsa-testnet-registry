"""drop trace_s3_key from deployment_browser_download_records

The column pointed at an S3 object `clients fetch-browser-traces` uploaded after
a run: one per download, `scp`-ed off every browser VM and then linked to its
row one `UPDATE` at a time. On DEV-01 (registry id 685) that step ran 2 h 47 min
for 2,676 files (2.5 GB) across 35 VMs, pushing the run past its ceiling and
leaving the node fleet to fill its disks while it worked.

The harness now appends every trace record to the forwarded log its VM already
tails, so the records reach the run's Elasticsearch as they are produced and
`analysis browser-reads` queries them there before teardown (V2-1650). There is
no archive, no fetch command and nothing to point at.

The downgrade re-adds the column as nullable, which is exactly what it was —
but every value is gone with the command that wrote them, and the objects they
named are not re-created. A downgrade restores the schema, never the data.

Revision ID: d1a6f03b7c58
Revises: c83d16f40b9d
Create Date: 2026-10-20

"""

import sqlalchemy as sa
from alembic import op

revision = "d1a6f03b7c58"
down_revision = "c83d16f40b9d"
branch_labels = None
depends_on = None

_TABLE = "deployment_browser_download_records"


def upgrade() -> None:
    op.drop_column(_TABLE, "trace_s3_key")


def downgrade() -> None:
    op.add_column(_TABLE, sa.Column("trace_s3_key", sa.Text(), nullable=True))
