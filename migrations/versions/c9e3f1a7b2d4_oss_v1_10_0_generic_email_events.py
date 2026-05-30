"""[oss-v1.10.0] generalize invoice_email_events -> email_events (polymorph)
+ add email tracking fields to meter_reading_access_codes

Macht den E-Mail-Versand-Audit-Trail mehrfach nutzbar: die bisher rein an
Rechnungen gekoppelte Tabelle ``invoice_email_events`` wird zur polymorphen
``email_events`` (``subject_type``/``subject_id``). Bestandsdaten werden als
``subject_type='invoice'`` uebernommen. Self-Service-Zugangscodes bekommen
dieselben sechs Tracking-Spalten wie Rechnungen.

Strategie: neue Tabelle anlegen + Daten kopieren + alte droppen. Das ist ueber
SQLite / MariaDB / Postgres portabel (kein in-place Table/Constraint-Rename,
keine SQLite-Batch-Recreate-Fallstricke). ``id``-Werte werden bewusst NICHT
uebernommen — auf ``email_events.id`` zeigt kein FK, und explizite IDs wuerden
auf Postgres die Identity-Sequence desynchronisieren.

Revision ID: c9e3f1a7b2d4
Revises: b8d2f4a6c1e9
Create Date: 2026-05-30

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c9e3f1a7b2d4'
down_revision = 'b8d2f4a6c1e9'
branch_labels = None
depends_on = None


def upgrade():
    # --- 1) meter_reading_access_codes: E-Mail-Tracking-Spalten ---------------
    with op.batch_alter_table("meter_reading_access_codes") as batch_op:
        batch_op.add_column(sa.Column("email_message_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("email_sent_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("email_recipient", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("last_email_status", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("last_email_status_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("last_email_bounce_detail", sa.String(length=512), nullable=True))
        batch_op.create_index("ix_mrac_email_message_id", ["email_message_id"], unique=False)

    # --- 2) email_events (polymorph) neu anlegen -----------------------------
    op.create_table(
        "email_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("record_type", sa.String(length=32), nullable=False),
        sa.Column("postmark_message_id", sa.String(length=128), nullable=True),
        sa.Column("recipient", sa.String(length=255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("bounce_type", sa.String(length=64), nullable=True),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "postmark_message_id",
            "record_type",
            name="uq_email_events_msgid_type",
        ),
    )
    op.create_index("ix_email_events_subject", "email_events",
                    ["subject_type", "subject_id"], unique=False)
    op.create_index("ix_email_events_postmark_message_id", "email_events",
                    ["postmark_message_id"], unique=False)

    # --- 3) Bestandsdaten uebernehmen (alle bisherigen Events = Rechnungen) ---
    op.execute(
        "INSERT INTO email_events "
        "(subject_type, subject_id, record_type, postmark_message_id, recipient, "
        " occurred_at, received_at, bounce_type, description, payload_json) "
        "SELECT 'invoice', invoice_id, record_type, postmark_message_id, recipient, "
        " occurred_at, received_at, bounce_type, description, payload_json "
        "FROM invoice_email_events"
    )

    # --- 4) alte Tabelle entfernen -------------------------------------------
    op.drop_index("ix_invoice_email_events_postmark_message_id", table_name="invoice_email_events")
    op.drop_index("ix_invoice_email_events_invoice_id", table_name="invoice_email_events")
    op.drop_table("invoice_email_events")


def downgrade():
    # --- alte Tabelle wiederherstellen ---------------------------------------
    op.create_table(
        "invoice_email_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=False),
        sa.Column("record_type", sa.String(length=32), nullable=False),
        sa.Column("postmark_message_id", sa.String(length=128), nullable=True),
        sa.Column("recipient", sa.String(length=255), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("bounce_type", sa.String(length=64), nullable=True),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "postmark_message_id",
            "record_type",
            name="uq_invoice_email_events_msgid_type",
        ),
    )
    op.create_index("ix_invoice_email_events_invoice_id", "invoice_email_events",
                    ["invoice_id"], unique=False)
    op.create_index("ix_invoice_email_events_postmark_message_id", "invoice_email_events",
                    ["postmark_message_id"], unique=False)

    # Nur die Rechnungs-Events zuruecksichern (andere subject_types gehen verloren).
    op.execute(
        "INSERT INTO invoice_email_events "
        "(invoice_id, record_type, postmark_message_id, recipient, "
        " occurred_at, received_at, bounce_type, description, payload_json) "
        "SELECT subject_id, record_type, postmark_message_id, recipient, "
        " occurred_at, received_at, bounce_type, description, payload_json "
        "FROM email_events WHERE subject_type = 'invoice'"
    )

    op.drop_index("ix_email_events_postmark_message_id", table_name="email_events")
    op.drop_index("ix_email_events_subject", table_name="email_events")
    op.drop_table("email_events")

    with op.batch_alter_table("meter_reading_access_codes") as batch_op:
        batch_op.drop_index("ix_mrac_email_message_id")
        batch_op.drop_column("last_email_bounce_detail")
        batch_op.drop_column("last_email_status_at")
        batch_op.drop_column("last_email_status")
        batch_op.drop_column("email_recipient")
        batch_op.drop_column("email_sent_at")
        batch_op.drop_column("email_message_id")
