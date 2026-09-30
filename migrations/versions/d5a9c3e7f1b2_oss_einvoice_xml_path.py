"""[oss] E-Rechnung: invoices.xml_path + invoices.einvoice_profile

Das EN-16931-XML (ZUGFeRD/Factur-X) einer Rechnung wird beim Versand
eingefroren und neben dem PDF abgelegt (``<PDF_DIR>/<Jahr>/<Nr>.xml``);
``xml_path`` zeigt darauf, ``einvoice_profile`` haelt das Profil
(``en16931``). Beide nullable — Altbelege und Entwuerfe haben keins.

Revision ID: d5a9c3e7f1b2
Revises: c3e8a1f6d2b9
Create Date: 2026-09-30 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd5a9c3e7f1b2'
down_revision = 'c3e8a1f6d2b9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('xml_path', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('einvoice_profile', sa.String(length=40), nullable=True))


def downgrade():
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_column('einvoice_profile')
        batch_op.drop_column('xml_path')
