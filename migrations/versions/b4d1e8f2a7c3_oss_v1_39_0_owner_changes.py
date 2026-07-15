"""[oss-v1.39.0] owner changes + invoice kind

Eigentuemerwechsel-Workflow + unterjaehrige Schlussrechnungen. Neu:
``owner_changes`` (Stichtag-Event je Objekt + optionaler Verweis auf die
Schlussrechnung, Grundgebuehr-Modus, abgerechnete Gebuehr-Tage) und
``owner_change_meter_values`` (Zaehler-Snapshot: Stand am Stichtag + dem
Altbesitzer verrechneter Verbrauch). Zusaetzlich die Spalte
``invoices.invoice_kind`` ('standard' | 'final_settlement'), die den
Massen-Rechnungslauf steuert (nur ``standard`` blockiert/laeuft; die
Schlussrechnung reduziert den spaeteren Jahresverbrauch des Nachbesitzers).

Additiv und dialekt-portabel (SQLite/MariaDB/Postgres): zwei neue Tabellen +
eine Spalte mit ``server_default``; Indices via ``batch_op.f(...)``. FK
``settlement_invoice_id`` ist ``ondelete='SET NULL'`` (feuert auf SQLite ohne
FK-Pragma nicht — der Deduktions-Join filtert daher zusaetzlich auf einen
nicht-stornierten Invoice und ``invoices.delete`` nullt die Verweise selbst).

Revision ID: b4d1e8f2a7c3
Revises: c3f8a1d5e9b2
Create Date: 2026-07-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b4d1e8f2a7c3'
down_revision = 'c3f8a1d5e9b2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'invoice_kind', sa.String(length=20), nullable=False,
            server_default=sa.text("'standard'")))

    op.create_table(
        'owner_changes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('property_id', sa.Integer(), nullable=False),
        sa.Column('billing_period_id', sa.Integer(), nullable=False),
        sa.Column('change_date', sa.Date(), nullable=False),
        sa.Column('settlement_invoice_id', sa.Integer(), nullable=True),
        sa.Column('base_fee_mode', sa.String(length=20),
                  server_default=sa.text("'new_owner_full'"), nullable=False),
        sa.Column('fee_days_billed', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id']),
        sa.ForeignKeyConstraint(['billing_period_id'], ['billing_periods.id']),
        sa.ForeignKeyConstraint(['settlement_invoice_id'], ['invoices.id'],
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('owner_changes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_owner_changes_property_id'),
                              ['property_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_owner_changes_billing_period_id'),
                              ['billing_period_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_owner_changes_settlement_invoice_id'),
                              ['settlement_invoice_id'], unique=False)

    op.create_table(
        'owner_change_meter_values',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_change_id', sa.Integer(), nullable=False),
        sa.Column('meter_id', sa.Integer(), nullable=False),
        sa.Column('value_at_change', sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column('consumption_billed', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('is_estimated', sa.Boolean(),
                  server_default=sa.text('false'), nullable=False),
        sa.ForeignKeyConstraint(['owner_change_id'], ['owner_changes.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['meter_id'], ['water_meters.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_change_id', 'meter_id',
                            name='uq_owner_change_meter'),
    )
    with op.batch_alter_table('owner_change_meter_values', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_owner_change_meter_values_owner_change_id'),
                              ['owner_change_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_owner_change_meter_values_meter_id'),
                              ['meter_id'], unique=False)


def downgrade():
    with op.batch_alter_table('owner_change_meter_values', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_owner_change_meter_values_meter_id'))
        batch_op.drop_index(batch_op.f('ix_owner_change_meter_values_owner_change_id'))
    op.drop_table('owner_change_meter_values')

    with op.batch_alter_table('owner_changes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_owner_changes_settlement_invoice_id'))
        batch_op.drop_index(batch_op.f('ix_owner_changes_billing_period_id'))
        batch_op.drop_index(batch_op.f('ix_owner_changes_property_id'))
    op.drop_table('owner_changes')

    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_column('invoice_kind')
