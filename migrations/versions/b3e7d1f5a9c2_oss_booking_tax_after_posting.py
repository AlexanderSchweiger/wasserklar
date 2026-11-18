"""Steuersatz nach dem Verbuchen + Steuersatz im Bankimport

* ``accounts.default_tax_rate``: Standard-Steuersatz eines Kontos (Vorschlag für Bankimport
  und Buchungsformular; NULL = keiner).
* ``bank_statement_lines.override_tax_rate``: im Bankimport gewählter Steuersatz einer Zeile
  ohne Rechnung (NULL = Standard des Kontos, 0 = bewusst ohne Steuer).
* ``booking_changes``: Protokoll nachträglicher Änderungen an verbuchten Buchungen
  (derzeit nur der Steuersatz).

Revision ID: b3e7d1f5a9c2
Revises: a9c4e2f6d8b1
Create Date: 2026-11-14
"""
from alembic import op
import sqlalchemy as sa


revision = "b3e7d1f5a9c2"
down_revision = "a9c4e2f6d8b1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("default_tax_rate", sa.Numeric(5, 2), nullable=True))
    with op.batch_alter_table("bank_statement_lines") as batch:
        batch.add_column(sa.Column("override_tax_rate", sa.Numeric(5, 2), nullable=True))
    op.create_table(
        "booking_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("booking_id", sa.Integer(), sa.ForeignKey("bookings.id", name="fk_booking_changes_booking"),
                  nullable=False),
        sa.Column("field", sa.String(length=30), nullable=False),
        sa.Column("old_value", sa.String(length=100), nullable=True),
        sa.Column("new_value", sa.String(length=100), nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        sa.Column("changed_by_id", sa.Integer(), sa.ForeignKey("users.id", name="fk_booking_changes_user"),
                  nullable=True),
    )
    op.create_index("ix_booking_changes_booking_id", "booking_changes", ["booking_id"])


def downgrade():
    op.drop_index("ix_booking_changes_booking_id", table_name="booking_changes")
    op.drop_table("booking_changes")
    with op.batch_alter_table("bank_statement_lines") as batch:
        batch.drop_column("override_tax_rate")
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("default_tax_rate")
