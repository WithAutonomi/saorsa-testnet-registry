"""add deployment saorsa_deploy_version

antops pins a `saorsa-deploy` version per job (V2-1626), so every run has to be
able to name the version of the tool that executed it. A Linear comment is not
where someone comparing two runs months later looks; the registry is, and the
tool version is a fact of the same kind as the `ant-node` version a run
deployed.

Nullable, and never backfilled: every row created before this column has no
answer, and a guess would read as a fact. `deployments up` writes it once when
it creates the row and no later command touches it, so the value is the version
that STARTED the run.

Revision ID: c83d16f40b9d
Revises: b4e17c9d2a06
Create Date: 2026-10-18

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c83d16f40b9d"
down_revision: Union[str, Sequence[str], None] = "b4e17c9d2a06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "deployments",
        sa.Column("saorsa_deploy_version", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("deployments", "saorsa_deploy_version")
