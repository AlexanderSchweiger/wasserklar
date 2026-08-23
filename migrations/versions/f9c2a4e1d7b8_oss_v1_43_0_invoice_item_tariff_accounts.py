"""[oss-v1.43.0] Kontierung: invoice_items.account_id, Tarif-Konten, billing_runs.project_id

Stellt die Buchungsdimension "Konto" auf der Rechnungsposition wieder her, die
in v1.7.0 (Revision ``e3b1f7a2c9d4``) ausgebaut worden war. Damit splittet die
Zahlung einer Rechnung wieder nach ``(account_id, project_id, tax_rate)`` in
eine Sammelbuchung — und faellt auf eine normale Einzelbuchung zurueck, wenn
alle Positionen dieselbe Kombination haben.

Neu dazu:

* ``water_tariffs.base_fee_account_id`` / ``additional_fee_account_id`` /
  ``price_per_m3_account_id`` — je Gebuehrenart ein Buchungskonto, das der
  Rechnungslauf auf die erzeugten Positionen schreibt.
* ``billing_runs.project_id`` — ein Projekt fuer den gesamten Lauf (z.B.
  "Wasserzins 2026"), das auf alle Positionen des Laufs gesetzt wird.

Rein additiv und dialekt-portabel (SQLite/MariaDB/Postgres): ausschliesslich
nullable Spalten via ``batch_alter_table`` (Pflicht fuer SQLite-ALTER). Kein
Backfill — Bestandsrechnungen bleiben unkontiert und fragen das Konto beim
Bezahlen ab.

Die FKs bekommen explizite Namen, sonst kann SQLite sie im Batch-Mode beim
Downgrade nicht wieder aufloesen (unbenannte Constraints).

Revision ID: f9c2a4e1d7b8
Revises: a3f7d2c9b6e1
Create Date: 2026-08-23 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f9c2a4e1d7b8'
down_revision = 'a3f7d2c9b6e1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('invoice_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('account_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_invoice_items_account_id', 'accounts', ['account_id'], ['id'])

    with op.batch_alter_table('water_tariffs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('base_fee_account_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('additional_fee_account_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('price_per_m3_account_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_water_tariffs_base_fee_account_id', 'accounts', ['base_fee_account_id'], ['id'])
        batch_op.create_foreign_key(
            'fk_water_tariffs_additional_fee_account_id', 'accounts',
            ['additional_fee_account_id'], ['id'])
        batch_op.create_foreign_key(
            'fk_water_tariffs_price_per_m3_account_id', 'accounts',
            ['price_per_m3_account_id'], ['id'])

    with op.batch_alter_table('billing_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('project_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_billing_runs_project_id', 'projects', ['project_id'], ['id'])


def downgrade():
    with op.batch_alter_table('billing_runs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_billing_runs_project_id', type_='foreignkey')
        batch_op.drop_column('project_id')

    with op.batch_alter_table('water_tariffs', schema=None) as batch_op:
        batch_op.drop_constraint('fk_water_tariffs_price_per_m3_account_id', type_='foreignkey')
        batch_op.drop_constraint('fk_water_tariffs_additional_fee_account_id', type_='foreignkey')
        batch_op.drop_constraint('fk_water_tariffs_base_fee_account_id', type_='foreignkey')
        batch_op.drop_column('price_per_m3_account_id')
        batch_op.drop_column('additional_fee_account_id')
        batch_op.drop_column('base_fee_account_id')

    with op.batch_alter_table('invoice_items', schema=None) as batch_op:
        batch_op.drop_constraint('fk_invoice_items_account_id', type_='foreignkey')
        batch_op.drop_column('account_id')
