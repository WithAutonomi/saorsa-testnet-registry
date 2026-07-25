"""add prod disk survey + service audit tables

Standalone (no deployment_id FK) records for production node fleets, keyed by
the inventory identifier string (e.g. ant-prod-01) — prod fleets are driven by
inventory.json, not a Deployment row. `prod_disk_surveys` (+ per-service child
rows) store fleet-wide disk-pressure survey history; `prod_service_audit` is
the add/remove/reset audit trail written best-effort by `prod services`.

Revision ID: c1d2e3f4a5b6
Revises: f4c81b2ad907
Create Date: 2026-07-25 13:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'f4c81b2ad907'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'prod_disk_surveys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('inventory_name', sa.String(length=255), nullable=False),
        sa.Column('surveyed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('host_disk', sa.JSON(), nullable=False),
        sa.Column('host_count', sa.Integer(), nullable=False),
        sa.Column('service_count', sa.Integer(), nullable=False),
        sa.Column('total_data_bytes', sa.BigInteger(), nullable=False),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_prod_disk_surveys_inventory_name'),
        'prod_disk_surveys',
        ['inventory_name'],
        unique=False,
    )

    op.create_table(
        'prod_disk_survey_services',
        sa.Column(
            'id',
            sa.BigInteger().with_variant(sa.Integer(), 'sqlite'),
            nullable=False,
        ),
        sa.Column('survey_id', sa.Integer(), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('provider', sa.String(length=64), nullable=True),
        sa.Column('service_name', sa.String(length=64), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ['survey_id'], ['prod_disk_surveys.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_prod_disk_survey_services_survey_id'),
        'prod_disk_survey_services',
        ['survey_id'],
        unique=False,
    )

    op.create_table(
        'prod_service_audit',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('inventory_name', sa.String(length=255), nullable=False),
        sa.Column('host', sa.String(length=255), nullable=False),
        sa.Column('service_name', sa.String(length=64), nullable=False),
        sa.Column(
            'action',
            sa.Enum(
                'added', 'removed', 'reset', name='prodserviceauditaction'
            ),
            nullable=False,
        ),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_prod_service_audit_inventory_name'),
        'prod_service_audit',
        ['inventory_name'],
        unique=False,
    )
    op.create_index(
        op.f('ix_prod_service_audit_host'),
        'prod_service_audit',
        ['host'],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f('ix_prod_service_audit_host'), table_name='prod_service_audit'
    )
    op.drop_index(
        op.f('ix_prod_service_audit_inventory_name'),
        table_name='prod_service_audit',
    )
    op.drop_table('prod_service_audit')

    op.drop_index(
        op.f('ix_prod_disk_survey_services_survey_id'),
        table_name='prod_disk_survey_services',
    )
    op.drop_table('prod_disk_survey_services')

    op.drop_index(
        op.f('ix_prod_disk_surveys_inventory_name'),
        table_name='prod_disk_surveys',
    )
    op.drop_table('prod_disk_surveys')

    # Drop the enum type explicitly (Postgres leaves it behind after drop_table).
    sa.Enum(name='prodserviceauditaction').drop(op.get_bind(), checkfirst=True)