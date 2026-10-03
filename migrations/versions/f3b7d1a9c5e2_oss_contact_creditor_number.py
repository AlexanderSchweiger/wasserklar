"""Kontakte: eigener Nummernkreis fuer Lieferanten (creditor_number)

Bisher gab es nur die Kunden-/Mitgliedsnummer (``customers.customer_number``); Lieferanten
blieben ohne Nummer oder zogen von Hand eine aus dem Kundenkreis. Neu:

* ``customers.creditor_number`` — Lieferantennummer (Integer, unique, nullable).
* ``supplier_counters`` — Singleton-Zaehler (id=1) analog ``customer_counters``.

Bestehende Lieferanten werden nach ``id`` ab 70001 (DATEV-Kreditorenbereich, Standard von
``numbers.supplier_start``) durchnummeriert; eine bereits von Hand vergebene Kundennummer
bleibt unberuehrt. Portabel: SELECT + Einzel-UPDATE, kein Dialekt-SQL.

Revision ID: f3b7d1a9c5e2
Revises: e7a1c5d9b3f6
Create Date: 2026-10-03 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3b7d1a9c5e2'
down_revision = 'e7a1c5d9b3f6'
branch_labels = None
depends_on = None

_START = 70001


def upgrade():
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('creditor_number', sa.Integer(), nullable=True))
        batch_op.create_unique_constraint('uq_customers_creditor_number', ['creditor_number'])

    op.create_table(
        'supplier_counters',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('next_seq', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )

    bind = op.get_bind()
    customers = sa.table('customers', sa.column('id', sa.Integer),
                         sa.column('is_supplier', sa.Boolean),
                         sa.column('creditor_number', sa.Integer))
    ids = [row[0] for row in bind.execute(
        sa.select(customers.c.id)
        .where(customers.c.is_supplier.is_(True))
        .order_by(customers.c.id)
    )]
    nr = _START
    for customer_id in ids:
        bind.execute(customers.update().where(customers.c.id == customer_id)
                     .values(creditor_number=nr))
        nr += 1

    counters = sa.table('supplier_counters', sa.column('id', sa.Integer),
                        sa.column('next_seq', sa.Integer))
    op.bulk_insert(counters, [{'id': 1, 'next_seq': nr}])


def downgrade():
    op.drop_table('supplier_counters')
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_constraint('uq_customers_creditor_number', type_='unique')
        batch_op.drop_column('creditor_number')
