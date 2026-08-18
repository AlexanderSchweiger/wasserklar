"""[oss-v1.42.0] credit notes (Storno-Rechnung / Gutschrift)

Wird eine bereits versendete oder bezahlte Rechnung storniert, kann dazu ein
eigener Beleg ausgestellt werden: die Storno-Rechnung (Gutschrift). Sie ist
eine ganz normale ``invoices``-Zeile mit ``invoice_kind='credit_note'``,
gespiegelten (negativen) Positionen und einer eigenen fortlaufenden
Rechnungsnummer — eine ausgestellte Rechnung wird nie geloescht oder
ueberschrieben, das Storno ist immer ein zweiter Beleg.

Neu ist nur die Self-FK ``invoices.cancels_invoice_id`` auf die stornierte
Originalrechnung; sie traegt die UStG-§11-Pflichtangabe „Bezug auf die
urspruengliche Rechnung" und haengt Original und Gutschrift in der UI
aneinander. Der Wert ``'credit_note'`` fuer ``invoice_kind`` braucht kein
Schema-Update (freie String-Spalte, kein Check-Constraint/Enum).

Additiv und dialekt-portabel (SQLite/MariaDB/Postgres): eine nullable Spalte
+ Index + FK via ``batch_alter_table``; ``ondelete='SET NULL'`` (feuert auf
SQLite ohne FK-Pragma nicht — betrifft nur das Loeschen von Entwuerfen, und
ein Entwurf hat nie eine Storno-Rechnung).

Revision ID: a3f7d2c9b6e1
Revises: e7a2d4f9c1b8
Create Date: 2026-08-18 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3f7d2c9b6e1'
down_revision = 'e7a2d4f9c1b8'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cancels_invoice_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_invoices_cancels_invoice_id'),
                              ['cancels_invoice_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_invoices_cancels_invoice_id', 'invoices',
            ['cancels_invoice_id'], ['id'], ondelete='SET NULL')


def downgrade():
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_constraint('fk_invoices_cancels_invoice_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_invoices_cancels_invoice_id'))
        batch_op.drop_column('cancels_invoice_id')
