"""[oss-v1.6.0] add bank_statements and bank_statement_lines

Revision ID: afa553fa8c5a
Revises: d8a3c7e9f1b2
Create Date: 2026-05-24 22:09:01.384598

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'afa553fa8c5a'
down_revision = 'd8a3c7e9f1b2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bank_statements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('format', sa.String(length=20), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('file_hash', sa.String(length=64), nullable=False),
        sa.Column('real_account_id', sa.Integer(), nullable=False),
        sa.Column('statement_reference', sa.String(length=100), nullable=True),
        sa.Column('booking_date_from', sa.Date(), nullable=True),
        sa.Column('booking_date_to', sa.Date(), nullable=True),
        sa.Column('opening_balance', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('closing_balance', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), nullable=True),
        sa.Column('uploaded_by_id', sa.Integer(), nullable=True),
        sa.Column('committed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['real_account_id'], ['real_accounts.id']),
        sa.ForeignKeyConstraint(['uploaded_by_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('real_account_id', 'file_hash', name='uq_stmt_hash'),
    )
    with op.batch_alter_table('bank_statements', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bank_statements_file_hash'), ['file_hash'], unique=False)

    op.create_table(
        'bank_statement_lines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('statement_id', sa.Integer(), nullable=False),
        sa.Column('line_index', sa.Integer(), nullable=False),
        sa.Column('booking_date', sa.Date(), nullable=False),
        sa.Column('value_date', sa.Date(), nullable=True),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('counterparty_name', sa.String(length=200), nullable=True),
        sa.Column('counterparty_iban', sa.String(length=34), nullable=True),
        sa.Column('purpose', sa.Text(), nullable=True),
        sa.Column('end_to_end_id', sa.String(length=100), nullable=True),
        sa.Column('tx_id', sa.String(length=100), nullable=True),
        sa.Column('matched_invoice_id', sa.Integer(), nullable=True),
        sa.Column('matched_open_item_id', sa.Integer(), nullable=True),
        sa.Column('matched_customer_id', sa.Integer(), nullable=True),
        sa.Column('match_type', sa.String(length=20), nullable=True),
        sa.Column('override_account_id', sa.Integer(), nullable=True),
        sa.Column('selected', sa.Boolean(), nullable=False),
        sa.Column('line_status', sa.String(length=20), nullable=False),
        sa.Column('booking_id', sa.Integer(), nullable=True),
        sa.Column('booking_group_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['booking_group_id'], ['booking_groups.id']),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id']),
        sa.ForeignKeyConstraint(['matched_customer_id'], ['customers.id']),
        sa.ForeignKeyConstraint(['matched_invoice_id'], ['invoices.id']),
        sa.ForeignKeyConstraint(['matched_open_item_id'], ['open_items.id']),
        sa.ForeignKeyConstraint(['override_account_id'], ['accounts.id']),
        sa.ForeignKeyConstraint(['statement_id'], ['bank_statements.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('bank_statement_lines')
    with op.batch_alter_table('bank_statements', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bank_statements_file_hash'))
    op.drop_table('bank_statements')
