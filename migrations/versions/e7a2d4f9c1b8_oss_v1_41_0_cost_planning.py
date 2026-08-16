"""[oss-v1.41.0] add consumption_years + funding_goals tables (Plankostenrechnung)

Plankostenrechnung: aus einem Finanzierungsziel (Ruecklage ansparen oder Kredit
tilgen) drei Tarifpakete ableiten.

``consumption_years`` haelt die Jahres-Verbrauchssumme in m³ — zum einen als
Cache der gemessenen Summe (sonst muesste jede Auswertung ueber die komplette
``meter_readings``-Tabelle aggregieren), zum anderen als manuelle Eingabe
(``source='manual'``) fuer Jahre ohne Ablesungen ODER als bewusste
Uebersteuerung einer vorhandenen Messung. ``total_m3`` ist der wirksame Wert,
``measured_m3`` haelt daneben die berechnete Summe, damit eine Uebersteuerung
sichtbar bleibt und jederzeit aufgehoben werden kann.
Schluessel ist das Kalenderjahr und nicht ``billing_period_id``, damit
historische Jahre ohne Pseudo-Periode erfassbar sind; ``billing_period_id``
bleibt als optionaler Link (NULL bei rein manuellen Jahren).

``funding_goals`` haelt Zielbetrag, Laufzeit, Zinssatz und das beschlossene
Paket. Gerechnet wird nicht gespeichert, sondern bei jedem Aufruf neu, damit
ein geaenderter Verbrauch oder Tarif sofort durchschlaegt.

Additiv und dialekt-portabel (SQLite/MariaDB/Postgres): nur zwei neue Tabellen,
``server_default`` als ``sa.text(...)``, Indices via ``batch_op.f(...)``,
``ondelete='SET NULL'`` auf den weichen Verweisen (Periode, User) — eine
geloeschte Abrechnungsperiode darf die Verbrauchshistorie nicht mitreissen.

Revision ID: e7a2d4f9c1b8
Revises: d5c2b8f1e4a7
Create Date: 2026-08-15 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e7a2d4f9c1b8'
down_revision = 'd5c2b8f1e4a7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'consumption_years',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('billing_period_id', sa.Integer(), nullable=True),
        sa.Column('total_m3', sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column('measured_m3', sa.Numeric(precision=14, scale=3), nullable=True),
        sa.Column('source', sa.String(length=10),
                  server_default=sa.text("'measured'"), nullable=False),
        sa.Column('reading_count', sa.Integer(), nullable=True),
        sa.Column('unit_count', sa.Integer(), nullable=True),
        sa.Column('stale', sa.Boolean(),
                  server_default=sa.text('false'), nullable=False),
        sa.Column('computed_at', sa.DateTime(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['billing_period_id'], ['billing_periods.id'],
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('consumption_years', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_consumption_years_year'),
                              ['year'], unique=True)
        batch_op.create_index(
            batch_op.f('ix_consumption_years_billing_period_id'),
            ['billing_period_id'], unique=False)

    op.create_table(
        'funding_goals',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('goal_type', sa.String(length=20),
                  server_default=sa.text("'investment'"), nullable=False),
        sa.Column('target_amount', sa.Numeric(precision=12, scale=2),
                  nullable=False),
        sa.Column('existing_reserve', sa.Numeric(precision=12, scale=2),
                  server_default=sa.text('0'), nullable=False),
        sa.Column('interest_rate', sa.Numeric(precision=6, scale=3),
                  nullable=True),
        sa.Column('start_year', sa.Integer(), nullable=False),
        sa.Column('target_year', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20),
                  server_default=sa.text("'draft'"), nullable=False),
        sa.Column('chosen_scenario', sa.String(length=20), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('funding_goals')
    with op.batch_alter_table('consumption_years', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_consumption_years_billing_period_id'))
        batch_op.drop_index(batch_op.f('ix_consumption_years_year'))
    op.drop_table('consumption_years')
