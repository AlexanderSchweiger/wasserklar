"""[oss-v1.29.0] property geocoding columns (BEV address register)

Liegenschaften bekommen eine WGS84-Koordinate, damit der Leitungsplan jeden
Hausanschluss per Nearest-Neighbour der naechsten Liegenschaft zuordnen kann.
Befuellt werden die Spalten ueber den BEV-Adressregister-Index
(``flask bev-refresh``) und den "BEV-Adressen abgleichen"-Button.

Drei additive, nullbare Spalten auf ``properties``:
  - ``lat`` / ``lng`` (Float): WGS84-Koordinate (Leaflet-Reihenfolge lat/lng).
  - ``geocoded_at`` (DateTime): Zeitpunkt des letzten erfolgreichen Treffers;
    NULL = noch nicht oder nicht gefunden (Re-Abgleich versucht es erneut).

Rein additiv -- dialekt-portabel (SQLite/MariaDB/Postgres), kein PostGIS,
kein JSON-Typ. ``batch_alter_table`` fuer die SQLite-vertraegliche
Spaltenergaenzung.

Revision ID: b9f3a1c7e2d8
Revises: a7d2f9c4e1b8
Create Date: 2026-06-18

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b9f3a1c7e2d8'
down_revision = 'a7d2f9c4e1b8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('properties', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lat', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('lng', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('geocoded_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('properties', schema=None) as batch_op:
        batch_op.drop_column('geocoded_at')
        batch_op.drop_column('lng')
        batch_op.drop_column('lat')
