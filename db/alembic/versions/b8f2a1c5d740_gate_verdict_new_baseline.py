"""allow a gate verdict with no baseline (a new baseline run)

`deployments compare --new-baseline` records the verdict for a run that has no
comparable predecessor: the staging profile changed shape (the WASM client tier
was added and the NAT fraction dropped from 30% to 10%), so nothing recorded
before it is a valid baseline for anything recorded after it. Only the
single-run success floors are evaluated; every relative gate is NOT ASSESSED.

Two changes, and the second is the load-bearing one:

* `baseline_deployment_id` becomes nullable, because there genuinely is no
  baseline -- pointing it at the run itself, or at an arbitrary predecessor,
  would record a comparison that was never made.
* The existing unique constraint spans (deployment_id, baseline_deployment_id,
  window_label), and Postgres treats NULLs as DISTINCT -- so two NULL-baseline
  rows for the same run and window do not collide and a re-run would
  accumulate rows rather than replacing its previous answer. "The verdict for
  this run" would then be ambiguous at exactly the moment it matters. A PARTIAL
  unique index on (deployment_id, window_label) WHERE baseline_deployment_id IS
  NULL restores the one-row-per-comparison guarantee for that case.

Revision ID: b8f2a1c5d740
Revises: c3d81f0a95b2
"""

import sqlalchemy as sa
from alembic import op

revision = "b8f2a1c5d740"
down_revision = "c3d81f0a95b2"
branch_labels = None
depends_on = None

TABLE = "deployment_gate_verdicts"
PARTIAL_INDEX = "uq_deployment_gate_verdicts_new_baseline"


def upgrade() -> None:
    op.alter_column(
        TABLE,
        "baseline_deployment_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.create_index(
        PARTIAL_INDEX,
        TABLE,
        ["deployment_id", "window_label"],
        unique=True,
        postgresql_where=sa.text("baseline_deployment_id IS NULL"),
        sqlite_where=sa.text("baseline_deployment_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(PARTIAL_INDEX, table_name=TABLE)
    # Any new-baseline verdict has no baseline to restore, and the column is
    # about to become NOT NULL again. Drop those rows rather than inventing a
    # baseline id for them -- the verdict is re-derivable from stored data by
    # re-running `compare`, an invented comparison is not.
    op.execute(sa.text(f"DELETE FROM {TABLE} WHERE baseline_deployment_id IS NULL"))
    op.alter_column(
        TABLE,
        "baseline_deployment_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
