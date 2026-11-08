"""Liefer-/Leistungsdatum an der Rechnung

* ``invoices.service_date_from`` / ``service_date_to``: von Hand angegebener Tag
  oder Zeitraum der Lieferung/Leistung (NULL = wie bisher aus der
  Abrechnungsperiode abgeleitet — Bestand bleibt unveraendert).
* ``invoices.service_type``: Beschriftung auf dem Beleg (``delivery``/``service``).

Revision ID: a9c4e2f6d8b1
Revises: f6d2b8a4c0e7
Create Date: 2026-11-08
"""
from alembic import op
import sqlalchemy as sa


revision = "a9c4e2f6d8b1"
down_revision = "f6d2b8a4c0e7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invoices") as batch:
        batch.add_column(sa.Column("service_date_from", sa.Date(), nullable=True))
        batch.add_column(sa.Column("service_date_to", sa.Date(), nullable=True))
        batch.add_column(sa.Column("service_type", sa.String(length=10), nullable=True))


def downgrade():
    with op.batch_alter_table("invoices") as batch:
        batch.drop_column("service_type")
        batch.drop_column("service_date_to")
        batch.drop_column("service_date_from")
