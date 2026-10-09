"""Übergabe an die Steuerberatung (Gerüst): Zuordnungsfelder + Übergabeprotokoll

Neutrales Gerüst fuer einen Buchhaltungsexport an die Steuerberatung (z. B. DATEV in der
SaaS, später BMD). Das OSS selbst erzeugt keine Datei, es haelt nur:

* ``accounts.ledger_account`` / ``accounts.ledger_auto_tax`` — Sachkonto im Fremdprogramm
  und ob die Steuer dort im Konto steckt (Automatikkonto).
* ``real_accounts.ledger_account`` — Sachkonto des Geldkontos (Bank/Kasse).
* ``tax_rates.ledger_tax_key_output`` / ``ledger_tax_key_input`` — Steuerschlüssel fuer
  Einnahmen (Umsatzsteuer) bzw. Ausgaben (Vorsteuer).
* ``accounting_handovers`` + ``accounting_handover_items`` — Protokoll der Übergaben;
  eine aktive Übergabe sperrt die Kontierung ihrer Buchungen und Umbuchungen.

Rein additiv, alle neuen Spalten nullable bzw. mit Server-Default.

Revision ID: c5e1a9d3f7b2
Revises: a8d2e6f4c1b7
Create Date: 2026-10-09 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c5e1a9d3f7b2'
down_revision = 'a8d2e6f4c1b7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ledger_account', sa.String(length=9), nullable=True))
        batch_op.add_column(sa.Column('ledger_auto_tax', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))

    with op.batch_alter_table('real_accounts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ledger_account', sa.String(length=9), nullable=True))

    with op.batch_alter_table('tax_rates', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ledger_tax_key_output', sa.String(length=4), nullable=True))
        batch_op.add_column(sa.Column('ledger_tax_key_input', sa.String(length=4), nullable=True))

    op.create_table(
        'accounting_handovers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('format', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('fiscal_year', sa.Integer(), nullable=False),
        sa.Column('date_from', sa.Date(), nullable=False),
        sa.Column('date_to', sa.Date(), nullable=False),
        sa.Column('booking_count', sa.Integer(), nullable=False),
        sa.Column('transfer_count', sa.Integer(), nullable=False),
        sa.Column('document_count', sa.Integer(), nullable=False),
        sa.Column('total_amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('options', sa.Text(), nullable=True),
        sa.Column('file_name', sa.String(length=200), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
        sa.Column('withdrawn_at', sa.DateTime(), nullable=True),
        sa.Column('withdrawn_by_id', sa.Integer(), nullable=True),
        sa.Column('withdraw_reason', sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'],
                                name='fk_accounting_handovers_created_by'),
        sa.ForeignKeyConstraint(['withdrawn_by_id'], ['users.id'],
                                name='fk_accounting_handovers_withdrawn_by'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'accounting_handover_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('handover_id', sa.Integer(), nullable=False),
        sa.Column('booking_id', sa.Integer(), nullable=True),
        sa.Column('transfer_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['handover_id'], ['accounting_handovers.id'],
                                name='fk_accounting_handover_items_handover', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'],
                                name='fk_accounting_handover_items_booking'),
        sa.ForeignKeyConstraint(['transfer_id'], ['transfers.id'],
                                name='fk_accounting_handover_items_transfer'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('accounting_handover_items', schema=None) as batch_op:
        batch_op.create_index('ix_accounting_handover_items_handover_id', ['handover_id'])
        batch_op.create_index('ix_accounting_handover_items_booking_id', ['booking_id'])
        batch_op.create_index('ix_accounting_handover_items_transfer_id', ['transfer_id'])


def downgrade():
    with op.batch_alter_table('accounting_handover_items', schema=None) as batch_op:
        batch_op.drop_index('ix_accounting_handover_items_transfer_id')
        batch_op.drop_index('ix_accounting_handover_items_booking_id')
        batch_op.drop_index('ix_accounting_handover_items_handover_id')
    op.drop_table('accounting_handover_items')
    op.drop_table('accounting_handovers')

    with op.batch_alter_table('tax_rates', schema=None) as batch_op:
        batch_op.drop_column('ledger_tax_key_input')
        batch_op.drop_column('ledger_tax_key_output')

    with op.batch_alter_table('real_accounts', schema=None) as batch_op:
        batch_op.drop_column('ledger_account')

    with op.batch_alter_table('accounts', schema=None) as batch_op:
        batch_op.drop_column('ledger_auto_tax')
        batch_op.drop_column('ledger_account')
