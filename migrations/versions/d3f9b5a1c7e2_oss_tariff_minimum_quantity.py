"""[oss] Mindestabnahme im Tarif

* ``tariff_components.min_quantity`` — Mindestabnahme je Abrechnungszeitraum
  einer m³-Position (NULL = keine).
* ``minimum_overrides`` — individuelle Mindestabnahme je m³-Gebührenart fuer
  einen Kunden ODER ein Objekt (Menge ersetzt, NULL = keine Mindestabnahme).
  Eigene Tabelle statt Spalte an ``charge_overrides``: dort heisst eine Zeile
  „Betrag ersetzt“ — eine Zeile nur fuer die Menge wuerde den Preis kippen.
* ``invoice_items.is_minimum_fill`` (Auffuellzeile, zaehlt nicht zum
  Verbrauch) + ``invoice_items.min_quantity`` (effektive Mindestmenge,
  eingefroren fuer die Schaetzkorrektur).
* ``reading_corrections.min_quantity`` — Mindestmenge, die den
  Korrekturbetrag veraendert hat (Hinweis im Korrekturtext).

Regeln: ``app/invoices/tariff_engine.py``.

Revision ID: d3f9b5a1c7e2
Revises: b8e4c2a6f1d3
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa


revision = "d3f9b5a1c7e2"
down_revision = "b8e4c2a6f1d3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tariff_components", schema=None) as batch_op:
        batch_op.add_column(sa.Column("min_quantity", sa.Numeric(12, 3), nullable=True))

    with op.batch_alter_table("invoice_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_minimum_fill", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.add_column(sa.Column("min_quantity", sa.Numeric(12, 3), nullable=True))

    with op.batch_alter_table("reading_corrections", schema=None) as batch_op:
        batch_op.add_column(sa.Column("min_quantity", sa.Numeric(12, 3), nullable=True))

    op.create_table(
        "minimum_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("charge_type_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("property_id", sa.Integer(), nullable=True),
        sa.Column("min_quantity", sa.Numeric(12, 3), nullable=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["charge_type_id"], ["charge_types.id"], ondelete="CASCADE",
                                name="fk_minimum_overrides_charge_type_id"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE",
                                name="fk_minimum_overrides_customer_id"),
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"], ondelete="CASCADE",
                                name="fk_minimum_overrides_property_id"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("charge_type_id", "customer_id",
                            name="uq_minimum_overrides_customer"),
        sa.UniqueConstraint("charge_type_id", "property_id",
                            name="uq_minimum_overrides_property"),
    )
    op.create_index("ix_minimum_overrides_customer_id", "minimum_overrides", ["customer_id"])
    op.create_index("ix_minimum_overrides_property_id", "minimum_overrides", ["property_id"])


def downgrade():
    op.drop_index("ix_minimum_overrides_property_id", table_name="minimum_overrides")
    op.drop_index("ix_minimum_overrides_customer_id", table_name="minimum_overrides")
    op.drop_table("minimum_overrides")

    with op.batch_alter_table("reading_corrections", schema=None) as batch_op:
        batch_op.drop_column("min_quantity")

    with op.batch_alter_table("invoice_items", schema=None) as batch_op:
        batch_op.drop_column("min_quantity")
        batch_op.drop_column("is_minimum_fill")

    with op.batch_alter_table("tariff_components", schema=None) as batch_op:
        batch_op.drop_column("min_quantity")
