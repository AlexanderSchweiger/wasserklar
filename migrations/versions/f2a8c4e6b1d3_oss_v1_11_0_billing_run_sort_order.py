"""[oss-v1.11.0] add sort_order to billing_runs

Speichert die beim Rechnungslauf gewaehlte Sortierreihenfolge
(customer_name / customer_number / object_number / address) als
nullable String-Spalte in billing_runs.

Revision ID: f2a8c4e6b1d3
Revises: c9e3f1a7b2d4
Create Date: 2026-05-31

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f2a8c4e6b1d3'
down_revision = 'c9e3f1a7b2d4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('billing_runs', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('sort_order', sa.String(length=20), nullable=True)
        )


def downgrade():
    with op.batch_alter_table('billing_runs', schema=None) as batch_op:
        batch_op.drop_column('sort_order')
