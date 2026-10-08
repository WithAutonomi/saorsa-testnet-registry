"""add slack message ref columns to deployments

Revision ID: b7e1c9a4d2f8
Revises: 475b52cee53e
Create Date: 2026-06-01 15:45:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7e1c9a4d2f8"
down_revision: Union[str, Sequence[str], None] = "475b52cee53e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "deployments",
        sa.Column("slack_channel", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "deployments",
        sa.Column("slack_message_ts", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("deployments", "slack_message_ts")
    op.drop_column("deployments", "slack_channel")
