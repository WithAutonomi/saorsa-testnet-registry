"""add release_candidate tables

Revision ID: c2a14b3df7e1
Revises: add_canary_upload_cost
Create Date: 2026-05-20 17:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c2a14b3df7e1"
down_revision: Union[str, Sequence[str], None] = "add_canary_upload_cost"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Parent row: one per actual RC (e.g. "ant-node 0.11.1-rc.1").
    op.create_table(
        "release_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("binary_target", sa.String(length=64), nullable=False),
        sa.Column("binary_version", sa.String(length=64), nullable=False),
        sa.Column("rc_branch", sa.String(length=128), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "binary_target",
            "binary_version",
            name="uq_release_candidates_binary_version",
        ),
    )
    op.create_index(
        op.f("ix_release_candidates_binary_target"),
        "release_candidates",
        ["binary_target"],
        unique=False,
    )
    op.create_index(
        op.f("ix_release_candidates_rc_branch"),
        "release_candidates",
        ["rc_branch"],
        unique=False,
    )

    # Per-crate row under a release candidate.
    op.create_table(
        "release_candidate_crates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("release_candidate_id", sa.Integer(), nullable=False),
        sa.Column("crate_name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("repo_slug", sa.String(length=128), nullable=False),
        sa.Column("tag", sa.String(length=128), nullable=True),
        sa.Column("tag_sha", sa.String(length=64), nullable=True),
        sa.Column("since_tag", sa.String(length=128), nullable=True),
        sa.Column("since_sha", sa.String(length=64), nullable=True),
        sa.Column("head_sha", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["release_candidate_id"],
            ["release_candidates.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_candidate_id",
            "crate_name",
            name="uq_release_candidate_crates_rc_crate",
        ),
    )
    op.create_index(
        op.f("ix_release_candidate_crates_release_candidate_id"),
        "release_candidate_crates",
        ["release_candidate_id"],
        unique=False,
    )

    # PR row attached to a specific crate within an RC.
    op.create_table(
        "release_candidate_prs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("release_candidate_crate_id", sa.Integer(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("author", sa.String(length=128), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("url", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(
            ["release_candidate_crate_id"],
            ["release_candidate_crates.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_candidate_crate_id",
            "pr_number",
            name="uq_release_candidate_prs_crate_pr",
        ),
    )
    op.create_index(
        op.f("ix_release_candidate_prs_release_candidate_crate_id"),
        "release_candidate_prs",
        ["release_candidate_crate_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_release_candidate_prs_release_candidate_crate_id"),
        table_name="release_candidate_prs",
    )
    op.drop_table("release_candidate_prs")
    op.drop_index(
        op.f("ix_release_candidate_crates_release_candidate_id"),
        table_name="release_candidate_crates",
    )
    op.drop_table("release_candidate_crates")
    op.drop_index(
        op.f("ix_release_candidates_rc_branch"), table_name="release_candidates"
    )
    op.drop_index(
        op.f("ix_release_candidates_binary_target"),
        table_name="release_candidates",
    )
    op.drop_table("release_candidates")
