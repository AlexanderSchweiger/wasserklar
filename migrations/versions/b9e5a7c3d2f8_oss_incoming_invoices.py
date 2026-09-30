"""[oss] Eingangs-E-Rechnungen: Tabelle incoming_invoices

Empfangene E-Rechnungen (ZUGFeRD/XRechnung/UBL) mit dem unveraenderten Original als
Datei, den gelesenen Daten als JSON und der Verknuepfung zur Ausgabenbuchung.

Revision ID: b9e5a7c3d2f8
Revises: a8d4f6b2c1e7
Create Date: 2026-09-30 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b9e5a7c3d2f8'
down_revision = 'a8d4f6b2c1e7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'incoming_invoices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('original_name', sa.String(length=255), nullable=False),
        sa.Column('file_path', sa.String(length=500), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('source_kind', sa.String(length=10), nullable=False),
        sa.Column('syntax', sa.String(length=10), nullable=False),
        sa.Column('guideline', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('number', sa.String(length=100), nullable=True),
        sa.Column('issue_date', sa.Date(), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('type_code', sa.String(length=3), nullable=True),
        sa.Column('seller_name', sa.String(length=200), nullable=True),
        sa.Column('seller_vat_id', sa.String(length=30), nullable=True),
        sa.Column('grand_total', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('data', sa.Text(), nullable=False),
        sa.Column('supplier_id', sa.Integer(), nullable=True),
        sa.Column('booking_id', sa.Integer(), nullable=True),
        sa.Column('booking_group_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['booking_group_id'], ['booking_groups.id']),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['supplier_id'], ['customers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('incoming_invoices', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incoming_invoices_sha256'), ['sha256'], unique=False)


def downgrade():
    with op.batch_alter_table('incoming_invoices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_incoming_invoices_sha256'))
    op.drop_table('incoming_invoices')
