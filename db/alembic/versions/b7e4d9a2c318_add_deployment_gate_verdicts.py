"""add deployment_gate_verdicts

The staging release verdict is required to be computed by a deterministic tool
rather than an agent or a human, and to be reproducible from the same inputs.
This table is where that verdict is durably stored: Elasticsearch and InfluxDB
are per-run scratch and die with the testnet, so the registry Postgres is the
only system of record a verdict can survive in.

One row per (run, baseline, window). Re-running a comparison replaces in place —
the same rule deployment_derived_metrics uses, because "the verdict for this
run" must never be ambiguous.

threshold_config_version is stored because the thresholds live in versioned
config outside the tool (there is a quarterly recalibration review), so two
verdicts can legitimately disagree and the version is the only record of why.

Revision ID: b7e4d9a2c318
Revises: c1d2e3f4a5b6
Create Date: 2026-07-29 13:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7e4d9a2c318"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "deployment_gate_verdicts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.Integer(), nullable=False),
        sa.Column("baseline_deployment_id", sa.Integer(), nullable=False),
        sa.Column("window_label", sa.String(length=64), nullable=False),
        sa.Column("overall", sa.String(length=16), nullable=False),
        sa.Column("threshold_config_version", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.JSON(), nullable=False),
        sa.Column("report_markdown", sa.Text(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["deployment_id"], ["deployments.id"], ondelete="CASCADE"
        ),
        # Deliberately NO cascade on the baseline side: deleting a baseline
        # deployment must not erase the verdicts computed against it. The
        # verdict is the durable artifact; the baseline row is not needed to
        # read it.
        sa.ForeignKeyConstraint(["baseline_deployment_id"], ["deployments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deployment_id",
            "baseline_deployment_id",
            "window_label",
            name="uq_deployment_gate_verdicts_comparison",
        ),
    )
    op.create_index(
        op.f("ix_deployment_gate_verdicts_deployment_id"),
        "deployment_gate_verdicts",
        ["deployment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_deployment_gate_verdicts_baseline_deployment_id"),
        "deployment_gate_verdicts",
        ["baseline_deployment_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_deployment_gate_verdicts_baseline_deployment_id"),
        table_name="deployment_gate_verdicts",
    )
    op.drop_index(
        op.f("ix_deployment_gate_verdicts_deployment_id"),
        table_name="deployment_gate_verdicts",
    )
    op.drop_table("deployment_gate_verdicts")
