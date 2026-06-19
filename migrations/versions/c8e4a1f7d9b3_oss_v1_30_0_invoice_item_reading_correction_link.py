"""[oss-v1.30.0] link invoice_items to reading_corrections

Fuegt ``invoice_items.reading_correction_id`` (nullable FK -> reading_corrections,
ondelete SET NULL) hinzu. Damit laesst sich beim Loeschen eines Entwurfs bzw.
eines Rechnungslaufs der exakt in dieser Rechnung verrechnete Korrekturbetrag
der ``ReadingCorrection`` zurueckgeben (Carry-forward-Ledger bleibt konsistent;
zuvor ging beim Loeschen+Neu-Lauf ein bereits teilverrechneter Betrag verloren).

Rein additiv (eine nullbare Spalte + FK) — dialekt-portabel
(SQLite/MariaDB/Postgres), ``batch_alter_table`` fuer SQLite.

Revision ID: c8e4a1f7d9b3
Revises: b9f3a1c7e2d8
Create Date: 2026-06-19

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8e4a1f7d9b3'
down_revision = 'b9f3a1c7e2d8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('invoice_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('reading_correction_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_invoice_items_reading_correction_id',
            'reading_corrections',
            ['reading_correction_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('invoice_items', schema=None) as batch_op:
        batch_op.drop_constraint('fk_invoice_items_reading_correction_id', type_='foreignkey')
        batch_op.drop_column('reading_correction_id')
