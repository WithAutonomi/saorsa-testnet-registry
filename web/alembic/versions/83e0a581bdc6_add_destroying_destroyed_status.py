"""add_destroying_destroyed_status

Revision ID: 83e0a581bdc6
Revises: 8eee99add7e7
Create Date: 2026-02-26 23:45:32.794420

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "83e0a581bdc6"
down_revision: Union[str, Sequence[str], None] = "8eee99add7e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add 'destroying' and 'destroyed' values to the deploymentstatus enum."""
    op.execute("ALTER TYPE deploymentstatus ADD VALUE IF NOT EXISTS 'destroying'")
    op.execute("ALTER TYPE deploymentstatus ADD VALUE IF NOT EXISTS 'destroyed'")


def downgrade() -> None:
    """Remove 'destroying' and 'destroyed' values from the deploymentstatus enum.

    PostgreSQL does not support removing enum values directly, so we
    recreate the type without them.
    """
    op.execute("ALTER TYPE deploymentstatus RENAME TO deploymentstatus_old")
    op.execute(
        "CREATE TYPE deploymentstatus AS ENUM('pending', 'running', 'stopped', 'failed')"
    )
    op.execute(
        "ALTER TABLE deployments ALTER COLUMN status TYPE deploymentstatus "
        "USING status::text::deploymentstatus"
    )
    op.execute("DROP TYPE deploymentstatus_old")
