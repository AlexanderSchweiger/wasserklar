"""[oss-v1.28.0] estimated readings + reading corrections

Geschaetzte Zaehlerstaende: Schaetzung bei fehlender Ablesung, automatischer
Korrekturposten (Gutschrift/Nachforderung) bei Nachreichung des echten Stands.

Drei additive Aenderungen:
  - ``meter_readings.is_estimated`` (Boolean, NOT NULL, default False): markiert
    einen geschaetzten Stand. Bestandsdaten bekommen via ``server_default``
    automatisch False (= echte Ablesung).
  - ``invoice_items.is_estimated`` (Boolean, NOT NULL, default False): markiert
    eine Verbrauchsposition, die auf einer Schaetzung beruht ("geschätzt"-Badge).
  - neue Tabelle ``reading_corrections``: vorzeichenbehafteter Korrekturposten
    (``amount`` > 0 Nachforderung, < 0 Gutschrift) mit ``remaining_amount`` fuer
    den Carry-forward in Folgerechnungen (Gutschrift nie unter 0 -> Rest bleibt
    offen). FKs auf customers/water_meters/billing_periods/meter_readings/
    invoices (Quelle + Ziel) und users.

Rein additiv -- dialekt-portabel (SQLite/MariaDB/Postgres), kein PostGIS,
kein JSON-Typ. ``server_default`` als einfache Literale; ``batch_alter_table``
fuer die SQLite-vertraegliche Spaltenergaenzung.

Revision ID: a7d2f9c4e1b8
Revises: d3f7b9a2c4e1
Create Date: 2026-06-18

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7d2f9c4e1b8'
down_revision = 'd3f7b9a2c4e1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('meter_readings', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'is_estimated', sa.Boolean(),
            nullable=False, server_default=sa.false(),
        ))
    with op.batch_alter_table('invoice_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'is_estimated', sa.Boolean(),
            nullable=False, server_default=sa.false(),
        ))

    op.create_table(
        'reading_corrections',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('meter_id', sa.Integer(), nullable=False),
        sa.Column('billing_period_id', sa.Integer(), nullable=False),
        sa.Column('source_reading_id', sa.Integer(), nullable=True),
        sa.Column('source_invoice_id', sa.Integer(), nullable=True),
        sa.Column('applied_invoice_id', sa.Integer(), nullable=True),
        sa.Column('estimated_consumption', sa.Numeric(12, 3), nullable=True),
        sa.Column('real_consumption', sa.Numeric(12, 3), nullable=True),
        sa.Column('delta_m3', sa.Numeric(12, 3), nullable=True),
        sa.Column('unit_price', sa.Numeric(10, 4), nullable=False),
        sa.Column('tax_rate', sa.Numeric(5, 2), nullable=True),
        sa.Column('amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('remaining_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default=sa.text("'Offen'")),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'],
                                name='fk_reading_corrections_customer_id'),
        sa.ForeignKeyConstraint(['meter_id'], ['water_meters.id'],
                                name='fk_reading_corrections_meter_id'),
        sa.ForeignKeyConstraint(['billing_period_id'], ['billing_periods.id'],
                                name='fk_reading_corrections_billing_period_id'),
        sa.ForeignKeyConstraint(['source_reading_id'], ['meter_readings.id'],
                                name='fk_reading_corrections_source_reading_id',
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['source_invoice_id'], ['invoices.id'],
                                name='fk_reading_corrections_source_invoice_id',
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['applied_invoice_id'], ['invoices.id'],
                                name='fk_reading_corrections_applied_invoice_id',
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'],
                                name='fk_reading_corrections_created_by_id'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('reading_corrections', schema=None) as batch_op:
        batch_op.create_index(
            'ix_reading_corrections_customer_id', ['customer_id'], unique=False)


def downgrade():
    with op.batch_alter_table('reading_corrections', schema=None) as batch_op:
        batch_op.drop_index('ix_reading_corrections_customer_id')
    op.drop_table('reading_corrections')
    with op.batch_alter_table('invoice_items', schema=None) as batch_op:
        batch_op.drop_column('is_estimated')
    with op.batch_alter_table('meter_readings', schema=None) as batch_op:
        batch_op.drop_column('is_estimated')
