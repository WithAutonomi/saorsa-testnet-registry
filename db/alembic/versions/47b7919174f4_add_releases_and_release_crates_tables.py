"""add releases and release_crates tables

Channel state per release: what shipped to beta, what was promoted to stable,
and what the beta channel rolled past. A table of its own rather than lifecycle
columns on ``release_candidates`` — an rc.N respin cuts a new RC row, so a train
can have several RC rows but only ever ships one release.

Revision ID: 47b7919174f4
Revises: 049de8b9d169
Create Date: 2026-08-18 20:52:00.665723

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "47b7919174f4"
down_revision: Union[str, Sequence[str], None] = "049de8b9d169"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "releases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("binary_target", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("rc_branch", sa.String(length=128), nullable=False),
        sa.Column("release_candidate_id", sa.Integer(), nullable=True),
        sa.Column("beta_tag", sa.String(length=128), nullable=True),
        sa.Column("beta_tag_sha", sa.String(length=64), nullable=True),
        sa.Column("shipped_to_beta_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned_promotion_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_tag", sa.String(length=128), nullable=True),
        sa.Column("final_tag_sha", sa.String(length=64), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(
                "shipped_to_beta", "promoted", "rolled_past", name="releaseoutcome"
            ),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["release_candidate_id"], ["release_candidates.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "binary_target", "version", name="uq_releases_binary_version"
        ),
    )
    op.create_index(
        op.f("ix_releases_binary_target"), "releases", ["binary_target"], unique=False
    )
    op.create_index(op.f("ix_releases_outcome"), "releases", ["outcome"], unique=False)
    op.create_index(
        op.f("ix_releases_rc_branch"), "releases", ["rc_branch"], unique=False
    )
    op.create_index(
        op.f("ix_releases_release_candidate_id"),
        "releases",
        ["release_candidate_id"],
        unique=False,
    )
    op.create_table(
        "release_crates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("crate_name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("repo_slug", sa.String(length=128), nullable=False),
        sa.Column("tag", sa.String(length=128), nullable=True),
        sa.Column("tag_sha", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_id", "crate_name", name="uq_release_crates_release_crate"
        ),
    )
    op.create_index(
        op.f("ix_release_crates_release_id"),
        "release_crates",
        ["release_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_release_crates_release_id"), table_name="release_crates")
    op.drop_table("release_crates")
    op.drop_index(op.f("ix_releases_release_candidate_id"), table_name="releases")
    op.drop_index(op.f("ix_releases_rc_branch"), table_name="releases")
    op.drop_index(op.f("ix_releases_outcome"), table_name="releases")
    op.drop_index(op.f("ix_releases_binary_target"), table_name="releases")
    op.drop_table("releases")
    # Postgres leaves the enum type behind after the table is dropped.
    sa.Enum(name="releaseoutcome").drop(op.get_bind(), checkfirst=True)
