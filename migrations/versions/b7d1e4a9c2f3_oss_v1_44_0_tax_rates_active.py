"""[oss-v1.44.0] Steuersaetze einstellbar: tax_rates.active

Die USt-Saetze kommen nicht mehr aus einer Code-Konstante, sondern aus der
Tabelle ``tax_rates``, die der Mandant in den Einstellungen pflegt (siehe
``app/tax_service.py`` + Länderprofile in ``app/country.py``). Ein Satz wird
nie geloescht, sondern deaktiviert — Belege speichern den Satz als Zahl, die
Historie bleibt so unberuehrt.

Rein additiv: eine NOT-NULL-Spalte mit ``server_default`` (Bestandssaetze sind
damit aktiv), via ``batch_alter_table`` fuer SQLite.

Revision ID: b7d1e4a9c2f3
Revises: f9c2a4e1d7b8
Create Date: 2026-09-28 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7d1e4a9c2f3'
down_revision = 'f9c2a4e1d7b8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tax_rates', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'active', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    with op.batch_alter_table('tax_rates', schema=None) as batch_op:
        batch_op.drop_column('active')
