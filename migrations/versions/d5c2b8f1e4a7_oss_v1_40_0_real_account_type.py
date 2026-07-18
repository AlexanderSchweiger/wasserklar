"""[oss-v1.40.0] real account type (Bank / Kassa)

Neue Spalte ``real_accounts.account_type`` ('bank' | 'cash'). Genossenschaften,
die bar abrechnen, legen damit ein Kassa-Konto neben ihren Bankkonten an —
Buchungen, Umbuchungen (Bareinnahme → Bank), Jahresabschluss-Snapshot und
Auswertungen laufen unveraendert ueber ``RealAccount``.

Additiv und dialekt-portabel (SQLite/MariaDB/Postgres): eine Spalte mit
``server_default='bank'``, damit Bestandszeilen und Roh-INSERTs (SaaS-
Provisioner-Seeds) einen gueltigen Wert bekommen. Der ``server_default``
bleibt bewusst stehen — kein zweites ALTER, das auf SQLite Batch-Mode
erzwingen wuerde.

Revision ID: d5c2b8f1e4a7
Revises: b4d1e8f2a7c3
Create Date: 2026-07-18 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd5c2b8f1e4a7'
down_revision = 'b4d1e8f2a7c3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('real_accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('account_type', sa.String(length=10),
                                      nullable=False, server_default='bank'))


def downgrade():
    with op.batch_alter_table('real_accounts', schema=None) as batch_op:
        batch_op.drop_column('account_type')
