"""[oss] Nachkommastellen der Preise je m³ einstellbar

* ``charge_types.price_decimals`` — mit wie vielen Nachkommastellen (2–4) der
  Preis je m³ einer Gebührenart im Rechnungstext, in Formularen und auf der
  Rechnung erscheint. NOT NULL mit Server-Default 2 — auch der Bestand bekommt
  2: ein Preis, der wirklich mehr Stellen nutzt (0,0815 €/m³), erscheint
  trotzdem vollständig (Sicherheitsnetz in ``price_format``).
* ``invoice_items.price_decimals`` — der beim Entstehen der Position
  eingefrorene Wert. NULL = Altbeleg (Anzeige unverändert: 4 je m³, sonst 2).

Gerechnet wird unverändert mit 4 Stellen; Regeln in
``app/invoices/price_format.py``.

Revision ID: e2c8f4a6b9d1
Revises: b6d2f8a4c1e3
Create Date: 2026-10-10
"""
from alembic import op
import sqlalchemy as sa


revision = "e2c8f4a6b9d1"
down_revision = "b6d2f8a4c1e3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("charge_types", schema=None) as batch_op:
        batch_op.add_column(sa.Column("price_decimals", sa.SmallInteger(), nullable=False,
                                      server_default=sa.text("2")))
    with op.batch_alter_table("invoice_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("price_decimals", sa.SmallInteger(), nullable=True))


def downgrade():
    with op.batch_alter_table("invoice_items", schema=None) as batch_op:
        batch_op.drop_column("price_decimals")
    with op.batch_alter_table("charge_types", schema=None) as batch_op:
        batch_op.drop_column("price_decimals")
