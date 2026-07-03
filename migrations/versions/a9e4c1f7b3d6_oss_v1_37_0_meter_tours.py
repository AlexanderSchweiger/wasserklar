"""[oss-v1.37.0] add meter_tours + meter_tour_stops tables

Zaehlertausch-Touren: faellige Zaehler (Nacheichfrist) zu einer abfahrbaren
Route buendeln. ``meter_tours`` haelt Kopf + Startpunkt (Luftlinien-Routing),
``meter_tour_stops`` die geordneten Halte mit Status und optionaler
Verknuepfung auf das Tausch-Event (``meter_replacements``) und die
Pauschalen-Rechnung (``invoices``). Additiv und dialekt-portabel
(SQLite/MariaDB/Postgres): nur zwei neue Tabellen, ``server_default`` als
``sa.text(...)``, Indices via ``batch_op.f(...)``. Koordinaten liegen bewusst
NICHT auf dem Stop (immer live aus ``properties.lat/lng``).

Revision ID: a9e4c1f7b3d6
Revises: f3c1a9d2e7b5
Create Date: 2026-07-03 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a9e4c1f7b3d6'
down_revision = 'f3c1a9d2e7b5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'meter_tours',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('planned_date', sa.Date(), nullable=True),
        sa.Column('time_window', sa.String(length=100), nullable=True),
        sa.Column('status', sa.String(length=20),
                  server_default=sa.text("'planned'"), nullable=False),
        sa.Column('start_lat', sa.Float(), nullable=True),
        sa.Column('start_lng', sa.Float(), nullable=True),
        sa.Column('start_address', sa.String(length=300), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'meter_tour_stops',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tour_id', sa.Integer(), nullable=False),
        sa.Column('meter_id', sa.Integer(), nullable=False),
        sa.Column('property_id', sa.Integer(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20),
                  server_default=sa.text("'pending'"), nullable=False),
        sa.Column('replacement_id', sa.Integer(), nullable=True),
        sa.Column('invoice_id', sa.Integer(), nullable=True),
        sa.Column('notified_at', sa.DateTime(), nullable=True),
        sa.Column('skip_reason', sa.String(length=300), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['tour_id'], ['meter_tours.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['meter_id'], ['water_meters.id']),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id']),
        sa.ForeignKeyConstraint(['replacement_id'], ['meter_replacements.id'],
                                ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tour_id', 'meter_id', name='uq_tour_stop_meter'),
    )
    with op.batch_alter_table('meter_tour_stops', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_meter_tour_stops_tour_id'),
                              ['tour_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_meter_tour_stops_meter_id'),
                              ['meter_id'], unique=False)


def downgrade():
    with op.batch_alter_table('meter_tour_stops', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_meter_tour_stops_meter_id'))
        batch_op.drop_index(batch_op.f('ix_meter_tour_stops_tour_id'))
    op.drop_table('meter_tour_stops')
    op.drop_table('meter_tours')
