"""[oss] E-Rechnung: customers.order_reference, supplier_number, peppol_id (UBL / e-Rechnung.gv.at)

Fuer Rechnungen an Bundesdienststellen in Oesterreich (UBL 2.1 / Peppol BIS 3.0):
Auftragsreferenz (BT-13), die Lieferantennummer der Genossenschaft beim Bund (BT-29)
und die Peppol-ID der Dienststelle (BT-49, ``Schema:Kennung``). Alle nullable.

Revision ID: a8d4f6b2c1e7
Revises: f7c3e5a9b1d4
Create Date: 2026-09-30 17:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a8d4f6b2c1e7'
down_revision = 'f7c3e5a9b1d4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('order_reference', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('supplier_number', sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column('peppol_id', sa.String(length=60), nullable=True))


def downgrade():
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_column('peppol_id')
        batch_op.drop_column('supplier_number')
        batch_op.drop_column('order_reference')
