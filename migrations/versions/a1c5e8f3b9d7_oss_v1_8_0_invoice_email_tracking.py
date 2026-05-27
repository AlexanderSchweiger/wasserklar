"""[oss-v1.8.0] add invoice email tracking fields + invoice_email_events table

Revision ID: a1c5e8f3b9d7
Revises: e3b1f7a2c9d4
Create Date: 2026-05-27

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1c5e8f3b9d7'
down_revision = 'e3b1f7a2c9d4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("invoices") as batch_op:
        batch_op.add_column(sa.Column("email_message_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("email_sent_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("email_recipient", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("last_email_status", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("last_email_status_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("last_email_bounce_detail", sa.String(length=512), nullable=True))
        batch_op.create_index(
            "ix_invoices_email_message_id",
            ["email_message_id"],
            unique=False,
        )

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
    op.create_index(
        "ix_invoice_email_events_invoice_id",
        "invoice_email_events",
        ["invoice_id"],
        unique=False,
    )
    op.create_index(
        "ix_invoice_email_events_postmark_message_id",
        "invoice_email_events",
        ["postmark_message_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_invoice_email_events_postmark_message_id", table_name="invoice_email_events")
    op.drop_index("ix_invoice_email_events_invoice_id", table_name="invoice_email_events")
    op.drop_table("invoice_email_events")

    with op.batch_alter_table("invoices") as batch_op:
        batch_op.drop_index("ix_invoices_email_message_id")
        batch_op.drop_column("last_email_bounce_detail")
        batch_op.drop_column("last_email_status_at")
        batch_op.drop_column("last_email_status")
        batch_op.drop_column("email_recipient")
        batch_op.drop_column("email_sent_at")
        batch_op.drop_column("email_message_id")
