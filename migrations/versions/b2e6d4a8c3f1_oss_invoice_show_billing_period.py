"""[oss-v1.51.0] Abrechnungsperiode auf Einzelrechnungen wahlweise andrucken

* ``invoices.show_billing_period`` (nullable): NULL = andrucken wie bisher
  (Rechnungslauf, Schlussrechnung, Bestand), False = nicht andrucken. Bei
  manuell erstellten Rechnungen waehlt der Nutzer im Formular.

Revision ID: b2e6d4a8c3f1
Revises: a7d3e9b5c1f8
Create Date: 2026-11-24
"""
from alembic import op
import sqlalchemy as sa


revision = "b2e6d4a8c3f1"
down_revision = "a7d3e9b5c1f8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invoices") as batch_op:
        batch_op.add_column(sa.Column("show_billing_period", sa.Boolean(), nullable=True))


def downgrade():
    with op.batch_alter_table("invoices") as batch_op:
        batch_op.drop_column("show_billing_period")
