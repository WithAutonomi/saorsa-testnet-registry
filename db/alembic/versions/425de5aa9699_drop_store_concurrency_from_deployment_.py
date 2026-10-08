"""drop store_concurrency from deployment_prod_upload_results

Revision ID: 425de5aa9699
Revises: 444b257b6feb
Create Date: 2026-04-28 19:39:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "425de5aa9699"
down_revision: Union[str, Sequence[str], None] = "444b257b6feb"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column("deployment_prod_upload_results", "store_concurrency")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        "deployment_prod_upload_results",
        sa.Column("store_concurrency", sa.Integer(), nullable=True),
    )
