"""add failed_hosts and attempted_host_count to prod_disk_surveys

V2-1147: a survey makes one SSH probe per host and drops the host entirely when
it fails, so the fleet totals are computed from whatever remains — an
under-count that reads as reclaimed disk. Survey 39 (2026-09-01) showed node
data falling 28.1 -> 26.9 TiB; two hosts had simply dropped out, and across the
63 hosts present in both runs node data had grown 21.5 GiB.

Recording the failed set with the survey lets ``survey-report`` render the same
caveat retrospectively, instead of the failure existing only in the terminal
scrollback of whoever ran it.

Both columns are nullable with no default and no backfill: NULL means the
survey predates this change and its failed set is *unknown*, which is not the
same claim as an empty dict ("every host was reached"). ``attempted_host_count``
is stored rather than derived because ``host_count + len(failed_hosts)`` is not
reliably the attempt count — a host can answer SSH yet return no df line,
landing in neither.

Revision ID: a7e4c93b1f28
Revises: 47b7919174f4
Create Date: 2026-09-02

"""
import sqlalchemy as sa
from alembic import op

revision = "a7e4c93b1f28"
down_revision = "47b7919174f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prod_disk_surveys",
        sa.Column("failed_hosts", sa.JSON(), nullable=True),
    )
    op.add_column(
        "prod_disk_surveys",
        sa.Column("attempted_host_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("prod_disk_surveys", "attempted_host_count")
    op.drop_column("prod_disk_surveys", "failed_hosts")
