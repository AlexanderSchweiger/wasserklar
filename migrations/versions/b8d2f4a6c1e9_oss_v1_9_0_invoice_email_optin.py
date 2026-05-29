"""[oss-v1.9.0] invoice email opt-in: codes, consent log, admin notifications

Revision ID: b8d2f4a6c1e9
Revises: a1c5e8f3b9d7
Create Date: 2026-05-29

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8d2f4a6c1e9'
down_revision = 'a1c5e8f3b9d7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "invoice_email_optin_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id", name="uq_invoice_email_optin_customer"),
    )
    op.create_index(
        "ix_invoice_email_optin_codes_customer_id",
        "invoice_email_optin_codes",
        ["customer_id"],
        unique=False,
    )

    op.create_table(
        "customer_email_consent_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=True),
        sa.Column("consent_text_version", sa.String(length=20), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_customer_email_consent_log_customer_id",
        "customer_email_consent_log",
        ["customer_id"],
        unique=False,
    )

    op.create_table(
        "admin_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("level", sa.String(length=20), server_default="info", nullable=False),
        sa.Column("link_url", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_notifications_created_at",
        "admin_notifications",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "admin_notification_reads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("notification_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("read_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["notification_id"], ["admin_notifications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("notification_id", "user_id", name="uq_admin_notif_read_user"),
    )
    op.create_index(
        "ix_admin_notification_reads_notification_id",
        "admin_notification_reads",
        ["notification_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_notification_reads_user_id",
        "admin_notification_reads",
        ["user_id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_admin_notification_reads_user_id", table_name="admin_notification_reads")
    op.drop_index("ix_admin_notification_reads_notification_id", table_name="admin_notification_reads")
    op.drop_table("admin_notification_reads")

    op.drop_index("ix_admin_notifications_created_at", table_name="admin_notifications")
    op.drop_table("admin_notifications")

    op.drop_index("ix_customer_email_consent_log_customer_id", table_name="customer_email_consent_log")
    op.drop_table("customer_email_consent_log")

    op.drop_index("ix_invoice_email_optin_codes_customer_id", table_name="invoice_email_optin_codes")
    op.drop_table("invoice_email_optin_codes")
