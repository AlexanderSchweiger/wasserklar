"""[oss-v1.23.0] add meter_replacements event table

Explizites Zaehlertausch-Event (``meter_replacements``): alt->neu-Paarung +
Snapshot der Tausch-Metadaten (Endstand alt, Anfangsstand neu, Periode, Datum,
Ersteller). Ersetzt die fruehere Datums-Heuristik in ``_build_replacement_map``,
die bei zwei am selben Tag am selben Objekt getauschten Zaehlern nicht
aufloesbar war. ``old_meter_id`` ist unique (ein alter Zaehler wird hoechstens
einmal ersetzt). Kein Backfill: die Erkennung speist sich ab jetzt
ausschliesslich aus dieser Tabelle; neue Taeusche werden beim Buchen
protokolliert, Bestandstaeusche aus der Zeit davor erscheinen nicht.

Revision ID: e9c3a7b1d5f2
Revises: a2f8d4c6e1b9
Create Date: 2026-06-07

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e9c3a7b1d5f2'
down_revision = 'a2f8d4c6e1b9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'meter_replacements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('property_id', sa.Integer(), nullable=False),
        sa.Column('old_meter_id', sa.Integer(), nullable=False),
        sa.Column('new_meter_id', sa.Integer(), nullable=False),
        sa.Column('billing_period_id', sa.Integer(), nullable=False),
        sa.Column('replacement_date', sa.Date(), nullable=False),
        sa.Column('final_value', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('new_initial_value', sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id']),
        sa.ForeignKeyConstraint(['old_meter_id'], ['water_meters.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['new_meter_id'], ['water_meters.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['billing_period_id'], ['billing_periods.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('old_meter_id', name='uq_meter_replacement_old_meter'),
    )
    # Indizes im Batch-Modus (Konvention, laeuft auch auf SQLite durch).
    with op.batch_alter_table('meter_replacements', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_meter_replacements_property_id'), ['property_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_meter_replacements_new_meter_id'), ['new_meter_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_meter_replacements_billing_period_id'), ['billing_period_id'], unique=False)


def downgrade():
    with op.batch_alter_table('meter_replacements', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_meter_replacements_billing_period_id'))
        batch_op.drop_index(batch_op.f('ix_meter_replacements_new_meter_id'))
        batch_op.drop_index(batch_op.f('ix_meter_replacements_property_id'))
    op.drop_table('meter_replacements')
