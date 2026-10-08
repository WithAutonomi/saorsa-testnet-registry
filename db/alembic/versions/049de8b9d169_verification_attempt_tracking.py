"""verification attempt tracking

A failed or inconclusive read-back is now re-queued instead of being treated as
a final answer — one transient blip would otherwise brand an address failed
forever. Because a retry overwrites the row, these two columns carry what would
otherwise be lost: whether an address failed once or has been failing all week.

Revision ID: 049de8b9d169
Revises: ceed5f3648da
Create Date: 2026-08-15

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "049de8b9d169"
down_revision: Union[str, Sequence[str], None] = "ceed5f3648da"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default so existing rows get 1 rather than NULL, then dropped so
    # the application supplies the value from here on.
    op.add_column(
        "deployment_prod_upload_verifications",
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.alter_column(
        "deployment_prod_upload_verifications", "attempts", server_default=None
    )
    op.add_column(
        "deployment_prod_upload_verifications",
        sa.Column("first_failed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("deployment_prod_upload_verifications", "first_failed_at")
    op.drop_column("deployment_prod_upload_verifications", "attempts")
