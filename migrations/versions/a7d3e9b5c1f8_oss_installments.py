"""[oss-v1.51.0] Teilrechnungen (Akonto) mit Gegenverrechnung

* ``installment_plans`` / ``installment_plan_entries``: Teilbetragsplan je
  Periode, Liegenschaft und Rechnungsempfaenger mit seinen Faelligkeiten.
* ``invoices.installment_entry_id`` (Teilrechnung → Faelligkeit),
  ``invoices.mailed_with_invoice_id`` (Sammel-E-Mail: Hauptbeleg).
* ``invoice_items.deducts_invoice_id``: Gegenverrechnungs-Zeile der
  Jahres-/Schlussrechnung.
* ``billing_runs.run_kind`` (annual/installment), ``billing_runs.source_run_id``
  („gemeinsam versenden mit“), ``billing_runs.tariff_price_per_m3`` nullable.
* ``customers.installment_opt_out``.
* ``reading_corrections.kind`` (estimate/credit_carry).

Regeln in ``app/installments/``. Alle FKs/Indizes benannt.

Revision ID: a7d3e9b5c1f8
Revises: f6d2b8a4c0e7
Create Date: 2026-11-08
"""
from alembic import op
import sqlalchemy as sa


revision = "a7d3e9b5c1f8"
down_revision = "f6d2b8a4c0e7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "installment_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("billing_period_id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("tariff_id", sa.Integer(), nullable=True),
        sa.Column("basis_period_id", sa.Integer(), nullable=True),
        sa.Column("basis_consumption", sa.Numeric(12, 3), nullable=True),
        sa.Column("basis_source", sa.String(length=20), nullable=False,
                  server_default=sa.text("'reading'")),
        sa.Column("annual_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("installment_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("composition", sa.Text(), nullable=True),
        sa.Column("manual", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["billing_period_id"], ["billing_periods.id"],
                                name="fk_installment_plans_period"),
        sa.ForeignKeyConstraint(["property_id"], ["properties.id"],
                                name="fk_installment_plans_property"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"],
                                name="fk_installment_plans_customer"),
        sa.ForeignKeyConstraint(["tariff_id"], ["water_tariffs.id"],
                                name="fk_installment_plans_tariff", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["basis_period_id"], ["billing_periods.id"],
                                name="fk_installment_plans_basis_period",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"],
                                name="fk_installment_plans_created_by", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("billing_period_id", "property_id", "customer_id",
                            name="uq_installment_plans_period_property_customer"),
    )
    with op.batch_alter_table("installment_plans", schema=None) as batch_op:
        batch_op.create_index("ix_installment_plans_billing_period_id",
                              ["billing_period_id"], unique=False)
        batch_op.create_index("ix_installment_plans_property_id", ["property_id"],
                              unique=False)
        batch_op.create_index("ix_installment_plans_customer_id", ["customer_id"],
                              unique=False)

    op.create_table(
        "installment_plan_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.SmallInteger(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("skipped", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["plan_id"], ["installment_plans.id"],
                                name="fk_installment_plan_entries_plan", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("installment_plan_entries", schema=None) as batch_op:
        batch_op.create_index("ix_installment_plan_entries_plan_id", ["plan_id"],
                              unique=False)

    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.add_column(sa.Column("installment_entry_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("mailed_with_invoice_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_invoices_installment_entry",
                                    "installment_plan_entries",
                                    ["installment_entry_id"], ["id"], ondelete="SET NULL")
        batch_op.create_foreign_key("fk_invoices_mailed_with", "invoices",
                                    ["mailed_with_invoice_id"], ["id"], ondelete="SET NULL")
        batch_op.create_index("ix_invoices_installment_entry_id", ["installment_entry_id"],
                              unique=False)
        batch_op.create_index("ix_invoices_mailed_with_invoice_id",
                              ["mailed_with_invoice_id"], unique=False)

    with op.batch_alter_table("invoice_items", schema=None) as batch_op:
        batch_op.add_column(sa.Column("deducts_invoice_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_invoice_items_deducts_invoice", "invoices",
                                    ["deducts_invoice_id"], ["id"], ondelete="SET NULL")
        batch_op.create_index("ix_invoice_items_deducts_invoice_id",
                              ["deducts_invoice_id"], unique=False)

    with op.batch_alter_table("billing_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("run_kind", sa.String(length=20), nullable=False,
                                      server_default=sa.text("'annual'")))
        batch_op.add_column(sa.Column("source_run_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_billing_runs_source_run", "billing_runs",
                                    ["source_run_id"], ["id"], ondelete="SET NULL")
        batch_op.create_index("ix_billing_runs_source_run_id", ["source_run_id"],
                              unique=False)
        batch_op.alter_column("tariff_price_per_m3", existing_type=sa.Numeric(10, 4),
                              nullable=True)

    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("installment_opt_out", sa.Boolean(), nullable=False,
                                      server_default=sa.false()))

    with op.batch_alter_table("reading_corrections", schema=None) as batch_op:
        batch_op.add_column(sa.Column("kind", sa.String(length=20), nullable=False,
                                      server_default=sa.text("'estimate'")))


def downgrade():
    with op.batch_alter_table("reading_corrections", schema=None) as batch_op:
        batch_op.drop_column("kind")

    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.drop_column("installment_opt_out")

    # Teilrechnungslaeufe haben keinen Wasserpreis — vor dem NOT NULL fuellen.
    op.execute("UPDATE billing_runs SET tariff_price_per_m3 = 0 "
               "WHERE tariff_price_per_m3 IS NULL")
    with op.batch_alter_table("billing_runs", schema=None) as batch_op:
        batch_op.alter_column("tariff_price_per_m3", existing_type=sa.Numeric(10, 4),
                              nullable=False)
        batch_op.drop_index("ix_billing_runs_source_run_id")
        batch_op.drop_constraint("fk_billing_runs_source_run", type_="foreignkey")
        batch_op.drop_column("source_run_id")
        batch_op.drop_column("run_kind")

    with op.batch_alter_table("invoice_items", schema=None) as batch_op:
        batch_op.drop_index("ix_invoice_items_deducts_invoice_id")
        batch_op.drop_constraint("fk_invoice_items_deducts_invoice", type_="foreignkey")
        batch_op.drop_column("deducts_invoice_id")

    with op.batch_alter_table("invoices", schema=None) as batch_op:
        batch_op.drop_index("ix_invoices_mailed_with_invoice_id")
        batch_op.drop_index("ix_invoices_installment_entry_id")
        batch_op.drop_constraint("fk_invoices_mailed_with", type_="foreignkey")
        batch_op.drop_constraint("fk_invoices_installment_entry", type_="foreignkey")
        batch_op.drop_column("mailed_with_invoice_id")
        batch_op.drop_column("installment_entry_id")

    with op.batch_alter_table("installment_plan_entries", schema=None) as batch_op:
        batch_op.drop_index("ix_installment_plan_entries_plan_id")
    op.drop_table("installment_plan_entries")

    with op.batch_alter_table("installment_plans", schema=None) as batch_op:
        batch_op.drop_index("ix_installment_plans_customer_id")
        batch_op.drop_index("ix_installment_plans_property_id")
        batch_op.drop_index("ix_installment_plans_billing_period_id")
    op.drop_table("installment_plans")
