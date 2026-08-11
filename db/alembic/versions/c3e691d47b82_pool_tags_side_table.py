"""move pool tags from a JSON column to an indexed side table

Tags began as a JSON column on ``prod_file_pool``, filtered in Python after the
query returned. That meant a ``--tag`` lookup materialised every candidate row
before inspecting it, and a LIMIT could only be applied afterwards — so asking
for ten tagged files read the whole catalogue. Measured on a synthetic 200k-row
pool: 2.11s to return ten rows.

Backfilled here rather than later on purpose. The pool currently holds tens of
rows; once the local dataset lands it will hold hundreds of thousands, and this
migration only gets more expensive.

Tags are lower-cased on the way in so a lookup cannot miss on case alone, which
is what the repository already did when comparing.

Revision ID: c3e691d47b82
Revises: b2d58f0c4a11
Create Date: 2026-07-31

"""
import json

import sqlalchemy as sa
from alembic import op

revision = "c3e691d47b82"
down_revision = "b2d58f0c4a11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prod_file_pool_tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("tag", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"], ["prod_file_pool.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_id", "tag", name="uq_prod_file_pool_tags_file_tag"),
    )
    op.create_index(
        "ix_prod_file_pool_tags_file_id", "prod_file_pool_tags", ["file_id"]
    )
    # The lookup direction that matters: given a tag, which files carry it.
    op.create_index(
        "ix_prod_file_pool_tags_lookup", "prod_file_pool_tags", ["tag", "file_id"]
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, tags FROM prod_file_pool WHERE tags IS NOT NULL")
    ).fetchall()
    pairs = []
    for file_id, raw in rows:
        # Postgres JSON comes back decoded; SQLite hands back the text.
        values = json.loads(raw) if isinstance(raw, str) else raw
        if not values:
            continue
        for value in values:
            tag = str(value).strip().lower()
            if tag:
                pairs.append({"file_id": file_id, "tag": tag})
    # De-duplicate: the JSON column never enforced uniqueness, and the new
    # constraint does.
    seen = set()
    unique = []
    for pair in pairs:
        key = (pair["file_id"], pair["tag"])
        if key not in seen:
            seen.add(key)
            unique.append(pair)
    if unique:
        bind.execute(
            sa.text(
                "INSERT INTO prod_file_pool_tags (file_id, tag) "
                "VALUES (:file_id, :tag)"
            ),
            unique,
        )

    op.drop_column("prod_file_pool", "tags")


def downgrade() -> None:
    op.add_column("prod_file_pool", sa.Column("tags", sa.JSON(), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT file_id, tag FROM prod_file_pool_tags ORDER BY file_id, tag"
        )
    ).fetchall()
    by_file: dict = {}
    for file_id, tag in rows:
        by_file.setdefault(file_id, []).append(tag)
    for file_id, tags in by_file.items():
        bind.execute(
            sa.text("UPDATE prod_file_pool SET tags = :tags WHERE id = :id"),
            {"tags": json.dumps(tags), "id": file_id},
        )
    op.drop_index("ix_prod_file_pool_tags_lookup", table_name="prod_file_pool_tags")
    op.drop_index("ix_prod_file_pool_tags_file_id", table_name="prod_file_pool_tags")
    op.drop_table("prod_file_pool_tags")
