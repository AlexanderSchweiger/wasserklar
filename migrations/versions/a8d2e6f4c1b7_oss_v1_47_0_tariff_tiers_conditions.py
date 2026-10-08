"""[oss-v1.47.0] Tarifpositionen: Staffelpreise + Bedingungen

Drei Spalten an ``tariff_components``:

* ``tier_mode`` — ``graduated`` (anteilig: jede Stufe mit ihrem Preis) oder
  ``whole`` (Gesamtmenge zum Preis der erreichten Stufe). NOT NULL mit
  Server-Default, Bestand bekommt ``graduated`` (ohne Stufen wirkungslos).
* ``tiers`` — JSON-Text der weiteren Stufen ``[{"above": "200", "price": "1.5"}]``
  (der Betrag der Position ist der Preis der 1. Stufe). NULL = keine Staffel.
* ``conditions`` — JSON-Text ``{"contact_status": ["member"]}``. NULL = die
  Position gilt fuer jeden Rechnungsempfaenger.

JSON-in-Text statt eigener Tabellen: dialekt-portabel, reist ohne
Registry-Eintrag im data_transfer-Export mit, und weitere Bedingungen kommen
ohne Migration dazu. Regeln: ``app/invoices/tariff_spec.py``.

Revision ID: a8d2e6f4c1b7
Revises: f3b7d1a9c5e2
Create Date: 2026-10-08 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a8d2e6f4c1b7'
down_revision = 'f3b7d1a9c5e2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tariff_components', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tier_mode', sa.String(length=10), nullable=False,
                                      server_default=sa.text("'graduated'")))
        batch_op.add_column(sa.Column('tiers', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('conditions', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('tariff_components', schema=None) as batch_op:
        batch_op.drop_column('conditions')
        batch_op.drop_column('tiers')
        batch_op.drop_column('tier_mode')
