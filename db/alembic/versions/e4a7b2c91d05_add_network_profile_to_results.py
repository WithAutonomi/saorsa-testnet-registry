"""add network_profile to client op/result tables

Records which simulated residential network profile (V2-733) a client VM ran
under, so upload/download results are comparable across release trains without
joining through the deployment's ``deploy_args`` JSON.

Nullable with no default and no backfill: NULL means "unshaped", which is both
the default going forward and the truth for every row predating the feature.
Only the profile *name* is denormalised here — the resolved knob set (rates,
delay, jitter, loss, profile-set version) is stored once in ``deploy_args``,
which is what makes a past run interpretable after the profile file has moved on.

Revision ID: e4a7b2c91d05
Revises: d9f3a71c50e2
Create Date: 2026-07-30

"""
import sqlalchemy as sa
from alembic import op

revision = "e4a7b2c91d05"
down_revision = "d9f3a71c50e2"
branch_labels = None
depends_on = None

_TABLES = (
    "deployment_upload_records",
    "deployment_download_records",
    "deployment_prod_upload_results",
    "deployment_prod_download_results",
)


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("network_profile", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "network_profile")
