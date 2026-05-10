"""[oss-v1.2.0] add meter_type and parent_meter_id to water_meters

Revision ID: b4e7d9a1c8f3
Revises: 9a3f8b1c5d2e
Create Date: 2026-05-10 14:00:00.000000

Fuegt zwei Spalten an ``water_meters``:
  - ``meter_type`` (String(10), NOT NULL, default 'main'): Klassifizierung
    Hauptzaehler vs. Subzaehler. Bestandsdaten bekommen via ``server_default``
    automatisch 'main'.
  - ``parent_meter_id`` (Integer, FK -> water_meters.id, ondelete=SET NULL,
    nullable, indexed): Optionaler Verweis vom Subzaehler auf seinen
    Hauptzaehler. Maximal eine Ebene -- die Validierung "parent muss
    meter_type='main' sein" passiert in der Route, nicht im DB-Constraint
    (portabel ueber SQLite/MariaDB/Postgres).

Manuell geschrieben statt autogenerate, weil Self-Referencing-FK + SQLite
``batch_alter_table`` heikel ist und die FK-Reihenfolge sauber gesetzt sein
muss. Siehe wasserklaross/CLAUDE.md "Schema-Aenderungen (Alembic)".
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b4e7d9a1c8f3'
down_revision = '9a3f8b1c5d2e'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('water_meters') as batch_op:
        batch_op.add_column(sa.Column(
            'meter_type', sa.String(length=10),
            nullable=False, server_default=sa.text("'main'"),
        ))
        batch_op.add_column(sa.Column(
            'parent_meter_id', sa.Integer(), nullable=True,
        ))
        batch_op.create_index(
            'ix_water_meters_parent_meter_id', ['parent_meter_id'],
        )
        batch_op.create_foreign_key(
            'fk_water_meters_parent_meter_id',
            'water_meters',
            ['parent_meter_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('water_meters') as batch_op:
        batch_op.drop_constraint(
            'fk_water_meters_parent_meter_id', type_='foreignkey',
        )
        batch_op.drop_index('ix_water_meters_parent_meter_id')
        batch_op.drop_column('parent_meter_id')
        batch_op.drop_column('meter_type')
