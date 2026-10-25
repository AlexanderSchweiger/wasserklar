"""[oss] Zaehlerstaende auf der Rechnung (Verbrauchsnachweis)

* ``invoice_meter_readings``: je Rechnung und Zaehler Anfangs-/Endstand
  (Datum, Wert, geschaetzt, Ereignis Einbau/Ausbau/Eigentuemerwechsel) und
  der daraus verrechnete Verbrauch — eingefroren beim Entstehen der Rechnung,
  weil Ablesungen danach aenderbar bleiben.

Altbelege bekommen keine Zeilen (sie behalten ihr Aussehen); Entwuerfe ohne
Zeilen frieren beim Versenden ein. Regeln in ``app/invoices/meter_statement.py``.

Revision ID: f4b8d2a6c9e3
Revises: a3f7c9e1d5b2
Create Date: 2026-10-24
"""
from alembic import op
import sqlalchemy as sa


revision = "f4b8d2a6c9e3"
down_revision = "a3f7c9e1d5b2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "invoice_meter_readings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("meter_id", sa.Integer(), nullable=True),
        sa.Column("meter_number", sa.String(length=100), nullable=False),
        sa.Column("meter_type", sa.String(length=10), nullable=False,
                  server_default=sa.text("'main'")),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("start_value", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("start_estimated", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("start_event", sa.String(length=20), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("end_value", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("end_estimated", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("end_event", sa.String(length=20), nullable=True),
        sa.Column("consumption", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="CASCADE",
                                name="fk_invoice_meter_readings_invoice_id_invoices"),
        sa.ForeignKeyConstraint(["meter_id"], ["water_meters.id"], ondelete="SET NULL",
                                name="fk_invoice_meter_readings_meter_id_water_meters"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("invoice_meter_readings", schema=None) as batch_op:
        batch_op.create_index("ix_invoice_meter_readings_invoice_id", ["invoice_id"],
                              unique=False)


def downgrade():
    with op.batch_alter_table("invoice_meter_readings", schema=None) as batch_op:
        batch_op.drop_index("ix_invoice_meter_readings_invoice_id")
    op.drop_table("invoice_meter_readings")
