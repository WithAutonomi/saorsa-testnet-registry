"""add reason to deployment_prod_upload_results

Revision ID: 444b257b6feb
Revises: bd316bbc9871
Create Date: 2026-04-26 18:16:32.919761

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '444b257b6feb'
down_revision: Union[str, Sequence[str], None] = 'bd316bbc9871'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'deployment_prod_upload_results',
        sa.Column('reason', sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('deployment_prod_upload_results', 'reason')
