"""[oss-v1.50.0] Anteilsbuch (Genossenschaftsanteile am Mitglied)

* ``wg_share_entries``: datierte Bewegungen (Anfangsbestand, Erwerb,
  Uebertragung, Rueckgabe, Korrektur) je Kontakt. Der Bestand ist die Summe
  bis zum Stichtag. Nur aktiv, wenn ``org.share_binding`` = ``person``;
  ohne Einstellung bleiben die Anteile wie bisher an der Liegenschaft
  (keine Datenmigration).

Regeln in ``app/wg_shares.py``. Alle FKs/Indizes benannt (MariaDB kann
unbenannte FKs spaeter nicht gezielt droppen).

Revision ID: e5c9a3f7b1d4
Revises: d3f9b5a1c7e2
Create Date: 2026-11-01
"""
from alembic import op
import sqlalchemy as sa


revision = "e5c9a3f7b1d4"
down_revision = "d3f9b5a1c7e2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "wg_share_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("counterparty_customer_id", sa.Integer(), nullable=True),
        sa.Column("pair_entry_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False,
                  server_default=sa.text("'manual'")),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"],
                                name="fk_wg_share_entries_customer"),
        sa.ForeignKeyConstraint(["counterparty_customer_id"], ["customers.id"],
                                name="fk_wg_share_entries_counterparty",
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["pair_entry_id"], ["wg_share_entries.id"],
                                name="fk_wg_share_entries_pair", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"],
                                name="fk_wg_share_entries_created_by",
                                ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("wg_share_entries", schema=None) as batch_op:
        batch_op.create_index("ix_wg_share_entries_customer_date",
                              ["customer_id", "entry_date"], unique=False)
        batch_op.create_index("ix_wg_share_entries_counterparty_customer_id",
                              ["counterparty_customer_id"], unique=False)
        batch_op.create_index("ix_wg_share_entries_pair_entry_id",
                              ["pair_entry_id"], unique=False)


def downgrade():
    with op.batch_alter_table("wg_share_entries", schema=None) as batch_op:
        batch_op.drop_index("ix_wg_share_entries_pair_entry_id")
        batch_op.drop_index("ix_wg_share_entries_counterparty_customer_id")
        batch_op.drop_index("ix_wg_share_entries_customer_date")
    op.drop_table("wg_share_entries")
