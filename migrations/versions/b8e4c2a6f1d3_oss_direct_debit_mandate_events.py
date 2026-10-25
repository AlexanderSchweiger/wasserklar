"""[oss] Nachweis online erteilter SEPA-Lastschrift-Mandate

* ``direct_debit_mandate_events``: append-only Protokoll je Kontakt (Zeitpunkt,
  IP, Browser, Wortlaut des Mandats, Konto-Snapshot). Beim online erteilten
  Mandat tritt es an die Stelle der Unterschrift.

Regeln in ``app/direct_debit/mandate_form.py``.

Revision ID: b8e4c2a6f1d3
Revises: f4b8d2a6c9e3
Create Date: 2026-10-25
"""
from alembic import op
import sqlalchemy as sa


revision = "b8e4c2a6f1d3"
down_revision = "f4b8d2a6c9e3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "direct_debit_mandate_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("mandate_reference", sa.String(length=35), nullable=True),
        sa.Column("iban", sa.String(length=34), nullable=True),
        sa.Column("bic", sa.String(length=11), nullable=True),
        sa.Column("account_holder", sa.String(length=70), nullable=True),
        sa.Column("creditor_name", sa.String(length=200), nullable=True),
        sa.Column("creditor_id", sa.String(length=35), nullable=True),
        sa.Column("mandate_text", sa.Text(), nullable=True),
        sa.Column("mandate_text_version", sa.String(length=20), nullable=True),
        sa.Column("email", sa.String(length=120), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"],
                                name="fk_dd_mandate_events_customer", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("direct_debit_mandate_events", schema=None) as batch_op:
        batch_op.create_index("ix_direct_debit_mandate_events_customer_id", ["customer_id"],
                              unique=False)


def downgrade():
    with op.batch_alter_table("direct_debit_mandate_events", schema=None) as batch_op:
        batch_op.drop_index("ix_direct_debit_mandate_events_customer_id")
    op.drop_table("direct_debit_mandate_events")
