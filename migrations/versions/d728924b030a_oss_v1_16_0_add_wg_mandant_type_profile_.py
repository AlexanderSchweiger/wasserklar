"""oss-v1.16.0 add wg mandant type profile and function tables

Revision ID: d728924b030a
Revises: d4b9f2a1c6e8
Create Date: 2026-06-05 10:49:09.278198

Legt die drei Mandant-Typ-spezifischen Tabellen der Wassergenossenschaft an:
1:1-Profile zu Kontakt (Status, Mitglied-bis) und Liegenschaft (Anteile, m2)
sowie die mehrwertigen Funktionen (Obmann, Kassier, ...). Reine CREATE TABLEs —
dialekt-portabel (SQLite/MariaDB/Postgres), keine Aenderung an Bestandstabellen.

Hinweis: Autogenerate hatte zusaetzlich diverse SQLite-Server-Default-/Index-
Pseudo-Diffs an Bestandstabellen (billing_periods, meter_readings, roles, ...)
gemeldet — diese sind Artefakte und wurden bewusst entfernt.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd728924b030a'
down_revision = 'd4b9f2a1c6e8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'customer_wg_profiles',
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20),
                  server_default=sa.text("'member'"), nullable=False),
        sa.Column('member_until', sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
        sa.PrimaryKeyConstraint('customer_id'),
    )
    op.create_table(
        'property_wg_profiles',
        sa.Column('property_id', sa.Integer(), nullable=False),
        sa.Column('shares', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('area_m2', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ),
        sa.PrimaryKeyConstraint('property_id'),
    )
    op.create_table(
        'wg_functions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('function', sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('customer_id', 'function', name='uq_wg_function'),
    )
    op.create_index('ix_wg_functions_customer_id', 'wg_functions',
                    ['customer_id'], unique=False)


def downgrade():
    op.drop_index('ix_wg_functions_customer_id', table_name='wg_functions')
    op.drop_table('wg_functions')
    op.drop_table('property_wg_profiles')
    op.drop_table('customer_wg_profiles')
