"""add the pointer handover ops, outcomes and columns

The pointer client tier now hands pointers over (V2-1354): it drives `ant pointer
transfer`, checks the result with `finality` and `controller`, and proves the two
take-backs -- an `update` and a second `transfer` on the handed-over key -- are
refused BEFORE they pay. Those are four new ops.

`pointeroutcome` gains the three non-`final` finality statuses. They are
deliberately NOT folded into existing values: `settling` is a moment in a
handover's life, `unconfirmed` is final on a majority with a peer that did not
answer (the state a silent peer leaves behind, which `transfer` refuses to write
into), and `forked` is two rival final states and the one genuinely alarming
reading. Collapsing them into `error` or `stale` would make the one that matters
unfindable.

The six columns are nullable with no default and no backfill: the Influx points
behind every pre-handover row never carried these fields, so no past run can be
corrected. A NULL `recipient` is simply an operation that was not about a
handover.

The two counter columns widen from BIGINT to NUMERIC(20). A pointer counter is a
Rust `u64` and a handover writes `u64::MAX` (18446744073709551615), about twice
what a signed BIGINT holds -- so a redirect read's counter could not be stored at
all. Every pre-existing value is small, and NUMERIC(20) holds all of them, so the
widening cannot lose one; it is reversible for the same reason.

Enum values are added with `ADD VALUE IF NOT EXISTS` after an explicit COMMIT,
following `b2d58f0c4a11`: `ALTER TYPE ... ADD VALUE` cannot run inside a
transaction block on older servers. They are not removed on downgrade -- Postgres
cannot drop a value from an enum type, and an unused value is harmless.

The downgrade therefore KEEPS every row, as `b2d58f0c4a11`'s does: because the
enum values necessarily survive, a `transfer` or `finality` row stays perfectly
readable afterwards, and this build's model simply would not recognise it --
which is the position any pre-handover reader is in anyway. The one thing that
cannot survive is a counter above a signed BIGINT, so those two values are set
to NULL before the columns narrow. NULL is already what those columns mean by
"not recorded", and it is exactly what a BIGINT-era row would have carried.

Revision ID: a9d5f31e07c4
Revises: c7a4d15e83b2
Create Date: 2026-10-14

"""

import sqlalchemy as sa
from alembic import op

revision = "a9d5f31e07c4"
down_revision = "c7a4d15e83b2"
branch_labels = None
depends_on = None

_TABLE = "deployment_pointer_records"

#: Largest value a signed BIGINT holds. A pointer counter is a `u64`, so a
#: handover's `u64::MAX` is about twice this.
_MAX_BIGINT = 9223372036854775807

_NEW_OPS = ("transfer", "finality", "controller", "takeback")
_NEW_OUTCOMES = ("settling", "unconfirmed", "forked")

_NEW_COLUMNS = (
    ("recipient", sa.String(length=64)),
    ("finality_status", sa.String(length=32)),
    ("answered", sa.Integer()),
    ("group_size", sa.Integer()),
    ("holders", sa.Integer()),
    ("attempt", sa.String(length=16)),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("COMMIT")
        for value in _NEW_OPS:
            op.execute(f"ALTER TYPE pointerop ADD VALUE IF NOT EXISTS '{value}'")
        for value in _NEW_OUTCOMES:
            op.execute(f"ALTER TYPE pointeroutcome ADD VALUE IF NOT EXISTS '{value}'")

    for name, type_ in _NEW_COLUMNS:
        op.add_column(_TABLE, sa.Column(name, type_, nullable=True))

    for name in ("counter_expected", "counter_observed"):
        op.alter_column(
            _TABLE,
            name,
            type_=sa.Numeric(precision=20, scale=0),
            existing_type=sa.BigInteger(),
            existing_nullable=True,
        )


def downgrade() -> None:
    # Rows are KEPT, never deleted. The enum values cannot be dropped, so a
    # handover row remains readable after this runs; only the model stops
    # recognising it. Deleting the rows would throw away measurements to undo a
    # schema change, which is not a trade a downgrade gets to make.
    #
    # A counter above a signed BIGINT is the one value that cannot survive the
    # narrowing, so it goes to NULL -- what those columns already mean by "not
    # recorded", and what a BIGINT-era row would have carried.
    for name in ("counter_expected", "counter_observed"):
        op.execute(f"UPDATE {_TABLE} SET {name} = NULL WHERE {name} > {_MAX_BIGINT}")
    for name, _ in reversed(_NEW_COLUMNS):
        op.drop_column(_TABLE, name)

    for name in ("counter_expected", "counter_observed"):
        op.alter_column(
            _TABLE,
            name,
            type_=sa.BigInteger(),
            existing_type=sa.Numeric(precision=20, scale=0),
            existing_nullable=True,
        )
