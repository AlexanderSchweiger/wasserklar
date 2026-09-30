"""[oss] E-Rechnung: customers.vat_id, einvoice_format, buyer_reference

Fuer die XRechnung an Behoerden: USt-IdNr. des Kunden (BT-48), das gewuenschte
Format (NULL = ZUGFeRD-PDF, ``xrechnung``) und die Kaeuferreferenz (BT-10,
bei Behoerden die Leitweg-ID). Alle nullable — Bestandskunden aendern sich nicht.

Revision ID: e6b2d4f8a1c3
Revises: d5a9c3e7f1b2
Create Date: 2026-09-30 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e6b2d4f8a1c3'
down_revision = 'd5a9c3e7f1b2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('vat_id', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('einvoice_format', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('buyer_reference', sa.String(length=100), nullable=True))


def downgrade():
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_column('buyer_reference')
        batch_op.drop_column('einvoice_format')
        batch_op.drop_column('vat_id')
