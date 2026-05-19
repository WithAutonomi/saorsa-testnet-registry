"""add upload cost columns to prod_canary_uploads

Revision ID: add_canary_upload_cost
Revises: 5ab98e6205e0
Create Date: 2026-05-19 15:50:37.839462

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_canary_upload_cost'
down_revision: Union[str, Sequence[str], None] = '5ab98e6205e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Add per-upload cost telemetry surfaced by ``ant --json file upload``:

    * ``storage_cost_atto``  — ANT spent, in atto-ANT (10^-18). Stored as
      ``NUMERIC`` so future large uploads (which can produce uint256-sized
      values) don't overflow.
    * ``gas_cost_wei``       — gas spent in wei (10^-18 ETH). Same shape.
    * ``chunk_attempts_total`` — total per-chunk store attempts (after
      retries) — useful as a proxy for upload-time network friction.

    All nullable so already-recorded rows (PROD-CANARY-01) remain valid.
    """
    op.add_column(
        "prod_canary_uploads",
        sa.Column("storage_cost_atto", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "prod_canary_uploads",
        sa.Column("gas_cost_wei", sa.Numeric(), nullable=True),
    )
    op.add_column(
        "prod_canary_uploads",
        sa.Column("chunk_attempts_total", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("prod_canary_uploads", "chunk_attempts_total")
    op.drop_column("prod_canary_uploads", "gas_cost_wei")
    op.drop_column("prod_canary_uploads", "storage_cost_atto")
