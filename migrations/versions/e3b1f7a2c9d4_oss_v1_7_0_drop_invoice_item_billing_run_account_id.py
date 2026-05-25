"""[oss-v1.7.0] drop InvoiceItem.account_id and BillingRun.account_id

Revision ID: e3b1f7a2c9d4
Revises: afa553fa8c5a
Create Date: 2026-05-25

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'e3b1f7a2c9d4'
down_revision = 'afa553fa8c5a'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("billing_runs") as batch_op:
        batch_op.drop_column("account_id")

    with op.batch_alter_table("invoice_items") as batch_op:
        batch_op.drop_column("account_id")


def downgrade():
    import sqlalchemy as sa
    with op.batch_alter_table("billing_runs") as batch_op:
        batch_op.add_column(sa.Column("account_id", sa.Integer(), nullable=True))

    with op.batch_alter_table("invoice_items") as batch_op:
        batch_op.add_column(sa.Column("account_id", sa.Integer(), nullable=True))
