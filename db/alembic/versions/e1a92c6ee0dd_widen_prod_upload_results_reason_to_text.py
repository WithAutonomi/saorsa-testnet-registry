"""widen prod_upload_results.reason to text

Revision ID: e1a92c6ee0dd
Revises: 425de5aa9699
Create Date: 2026-04-29 20:13:44.454670

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1a92c6ee0dd"
down_revision: Union[str, Sequence[str], None] = "425de5aa9699"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "deployment_prod_upload_results",
        "reason",
        existing_type=sa.String(length=512),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        "deployment_prod_upload_results",
        "reason",
        existing_type=sa.Text(),
        type_=sa.String(length=512),
        existing_nullable=True,
    )
