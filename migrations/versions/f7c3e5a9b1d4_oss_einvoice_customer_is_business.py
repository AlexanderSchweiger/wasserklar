"""[oss] E-Rechnung: customers.is_business (Unternehmer-Kennzeichen)

Unternehmer i. S. d. UStG — ihnen duerfen in Deutschland nach der Uebergangszeit
nur noch E-Rechnungen zugestellt werden. NOT NULL mit ``server_default`` (wie
``is_company``): der Platform-Provisioner legt Zeilen per Roh-SQL an.

Revision ID: f7c3e5a9b1d4
Revises: e6b2d4f8a1c3
Create Date: 2026-09-30 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f7c3e5a9b1d4'
down_revision = 'e6b2d4f8a1c3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_business', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade():
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_column('is_business')
