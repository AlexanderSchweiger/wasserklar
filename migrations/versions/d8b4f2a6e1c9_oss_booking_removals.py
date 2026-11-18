"""Löschprotokoll für Buchungen (Festschreiben je Voranmeldungszeitraum)

Buchungen bleiben jetzt bis zum Sperrtag ihres Voranmeldungszeitraums offen und damit
löschbar (``app/accounting/posting.py``). ``booking_removals`` hält ein Abbild jeder
Buchung bzw. Sammelbuchung, die nicht mehr am Tag ihrer Erfassung gelöscht oder (bei
Sammelbuchungen) durch Bearbeiten ersetzt wird — append-only, ohne Fremdschlüssel auf die
Buchung.

Revision ID: d8b4f2a6e1c9
Revises: b3e7d1f5a9c2
Create Date: 2026-11-15
"""
from alembic import op
import sqlalchemy as sa


revision = "d8b4f2a6e1c9"
down_revision = "b3e7d1f5a9c2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "booking_removals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("original_booking_id", sa.Integer(), nullable=True),
        sa.Column("original_group_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=30), nullable=False),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("reference", sa.String(length=100), nullable=True),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("removed_at", sa.DateTime(), nullable=False),
        sa.Column("removed_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_booking_removals_user"), nullable=True),
    )


def downgrade():
    op.drop_table("booking_removals")
