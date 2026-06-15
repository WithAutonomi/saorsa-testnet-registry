"""add cost columns to deployment_prod_upload_results

Revision ID: add_prod_upload_cost
Revises: add_canary_unavail
Create Date: 2026-06-15 23:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_prod_upload_cost'
down_revision: Union[str, Sequence[str], None] = 'add_canary_unavail'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Add per-upload cost telemetry surfaced by ``ant --json file upload`` to the
    prod-uploaders result table (mirrors the prod_canary_uploads cost columns):

    * ``storage_cost_atto``  — ANT spent, in atto-ANT (10^-18). Stored as
      ``NUMERIC`` so large uploads (which can produce uint256-sized values)
      don't overflow.
    * ``gas_cost_wei``       — gas spent in wei (10^-18 ETH). Same shape.

    Both nullable so already-recorded rows remain valid and older ``ant``
    binaries (which omit the fields) keep working.
    """
    op.add_column(
        "deployment_prod_upload_results",
        sa.Column("storage_cost_atto", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "deployment_prod_upload_results",
        sa.Column("gas_cost_wei", sa.Numeric(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("deployment_prod_upload_results", "gas_cost_wei")
    op.drop_column("deployment_prod_upload_results", "storage_cost_atto")
