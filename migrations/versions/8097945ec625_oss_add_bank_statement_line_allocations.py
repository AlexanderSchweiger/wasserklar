"""[oss] add bank_statement_line_allocations

Aufteilung einer Bankzeile auf mehrere offene Posten (Sammelzahlung, die
mehrere Rechnungen begleicht).

Revision ID: 8097945ec625
Revises: d2a9f4c1b8e7
Create Date: 2026-06-20 19:38:26.138918

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '8097945ec625'
down_revision = 'd2a9f4c1b8e7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bank_statement_line_allocations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('line_id', sa.Integer(), nullable=False),
        sa.Column('open_item_id', sa.Integer(), nullable=True),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id']),
        sa.ForeignKeyConstraint(['line_id'], ['bank_statement_lines.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['open_item_id'], ['open_items.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('bank_statement_line_allocations', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_bank_statement_line_allocations_line_id'),
            ['line_id'], unique=False,
        )


def downgrade():
    with op.batch_alter_table('bank_statement_line_allocations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_bank_statement_line_allocations_line_id'))
    op.drop_table('bank_statement_line_allocations')
