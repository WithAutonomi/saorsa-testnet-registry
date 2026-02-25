"""create deployments table

Revision ID: 0f14f0fb7bf9
Revises:
Create Date: 2026-02-25 16:55:42.548629

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0f14f0fb7bf9'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "stopped", "failed", name="deploymentstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("workflow_run_id", sa.BigInteger(), nullable=False),
        sa.Column("deploy_args", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "deployment_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "deployment_id",
            sa.Integer(),
            sa.ForeignKey("deployments.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("deployment_events")
    op.drop_table("deployments")
    op.execute("DROP TYPE IF EXISTS deploymentstatus")
