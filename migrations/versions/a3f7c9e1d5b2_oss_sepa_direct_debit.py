"""[oss] SEPA-Lastschrift: Bankverbindung am Kontakt, Einzugslaeufe

* ``customers``: ``payment_method`` (``transfer`` | ``direct_debit``, NOT NULL,
  Server-Default ``transfer`` — der Bestand zahlt weiter per Ueberweisung),
  ``iban``, ``bic``, ``account_holder``, ``mandate_reference`` (eindeutig),
  ``mandate_signed_on``.
* ``invoices``: beim Verlassen des Entwurfs eingefrorene Zahlungsart samt
  Mandatsreferenz und belastetem Konto (NULL = Entwurf/Altbestand = Ueberweisung).
* ``direct_debit_runs`` / ``direct_debit_items``: Einzugslaeufe mit Snapshots,
  aus denen die pain.008-Datei jederzeit neu entsteht.

Regeln in ``app/direct_debit/``.

Revision ID: a3f7c9e1d5b2
Revises: e2c8f4a6b9d1
Create Date: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa


revision = "a3f7c9e1d5b2"
down_revision = "e2c8f4a6b9d1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("payment_method", sa.String(length=20), nullable=False,
                                      server_default=sa.text("'transfer'")))
        batch_op.add_column(sa.Column("iban", sa.String(length=34), nullable=True))
        batch_op.add_column(sa.Column("bic", sa.String(length=11), nullable=True))
        batch_op.add_column(sa.Column("account_holder", sa.String(length=70), nullable=True))
        batch_op.add_column(sa.Column("mandate_reference", sa.String(length=35), nullable=True))
        batch_op.add_column(sa.Column("mandate_signed_on", sa.Date(), nullable=True))
        batch_op.create_unique_constraint("uq_customers_mandate_reference", ["mandate_reference"])

    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("payment_method", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("debit_mandate_reference", sa.String(length=35), nullable=True))
        batch_op.add_column(sa.Column("debit_iban", sa.String(length=34), nullable=True))

    op.create_table(
        "direct_debit_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=35), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default=sa.text("'active'")),
        sa.Column("creditor_name", sa.String(length=70), nullable=True),
        sa.Column("creditor_id", sa.String(length=35), nullable=True),
        sa.Column("creditor_iban", sa.String(length=34), nullable=True),
        sa.Column("creditor_bic", sa.String(length=11), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_amount", sa.Numeric(precision=12, scale=2), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"],
                                name="fk_direct_debit_runs_created_by_id_users"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_direct_debit_runs_message_id"),
    )

    op.create_table(
        "direct_debit_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("open_item_id", sa.Integer(), nullable=True),
        sa.Column("invoice_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("collection_date", sa.Date(), nullable=False),
        sa.Column("end_to_end_id", sa.String(length=35), nullable=False),
        sa.Column("remittance", sa.String(length=140), nullable=True),
        sa.Column("debtor_name", sa.String(length=70), nullable=True),
        sa.Column("iban", sa.String(length=34), nullable=True),
        sa.Column("bic", sa.String(length=11), nullable=True),
        sa.Column("mandate_reference", sa.String(length=35), nullable=True),
        sa.Column("mandate_signed_on", sa.Date(), nullable=True),
        sa.Column("amended_from_iban", sa.String(length=34), nullable=True),
        sa.Column("amended_from_mandate", sa.String(length=35), nullable=True),
        sa.Column("returned_on", sa.Date(), nullable=True),
        sa.Column("return_reason", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["direct_debit_runs.id"], ondelete="CASCADE",
                                name="fk_direct_debit_items_run_id_direct_debit_runs"),
        sa.ForeignKeyConstraint(["open_item_id"], ["open_items.id"],
                                name="fk_direct_debit_items_open_item_id_open_items"),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"],
                                name="fk_direct_debit_items_invoice_id_invoices"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"],
                                name="fk_direct_debit_items_customer_id_customers"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("direct_debit_items", schema=None) as batch_op:
        batch_op.create_index("ix_direct_debit_items_run_id", ["run_id"], unique=False)
        batch_op.create_index("ix_direct_debit_items_open_item_id", ["open_item_id"], unique=False)
        batch_op.create_index("ix_direct_debit_items_invoice_id", ["invoice_id"], unique=False)
        batch_op.create_index("ix_direct_debit_items_customer_id", ["customer_id"], unique=False)


def downgrade():
    with op.batch_alter_table("direct_debit_items", schema=None) as batch_op:
        batch_op.drop_index("ix_direct_debit_items_customer_id")
        batch_op.drop_index("ix_direct_debit_items_invoice_id")
        batch_op.drop_index("ix_direct_debit_items_open_item_id")
        batch_op.drop_index("ix_direct_debit_items_run_id")
    op.drop_table("direct_debit_items")
    op.drop_table("direct_debit_runs")

    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.drop_column("debit_iban")
        batch_op.drop_column("debit_mandate_reference")
        batch_op.drop_column("payment_method")

    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.drop_constraint("uq_customers_mandate_reference", type_="unique")
        batch_op.drop_column("mandate_signed_on")
        batch_op.drop_column("mandate_reference")
        batch_op.drop_column("account_holder")
        batch_op.drop_column("bic")
        batch_op.drop_column("iban")
        batch_op.drop_column("payment_method")
