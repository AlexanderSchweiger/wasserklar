"""[oss-v1.1.0] add meter_reading_access_codes + self-service marker on readings

Revision ID: 9a3f8b1c5d2e
Revises: 7c7f225282c9
Create Date: 2026-05-10 12:00:00.000000

Fuegt die Tabelle ``meter_reading_access_codes`` und zwei Marker-Spalten an
``meter_readings`` hinzu. Wird genutzt von der SaaS-Self-Service-Erfassung
(``saas/self_service``) und ist in der Single-Tenant-OSS inert
(keine Routes lesen oder schreiben hier).

Manuell geschrieben (nicht autogeneriert), um ``server_default``,
``ondelete`` und Index-Reihenfolge zuverlaessig zu setzen — siehe
wasserklaross/CLAUDE.md "Schema-Aenderungen (Alembic)".
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9a3f8b1c5d2e'
down_revision = '7c7f225282c9'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Neue Tabelle anlegen — muss vor dem FK-add auf meter_readings stehen.
    op.create_table(
        'meter_reading_access_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=16), nullable=False),
        sa.Column('code_hash', sa.String(length=255), nullable=False),
        sa.Column('expires_at', sa.Date(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_ip', sa.String(length=64), nullable=True),
        sa.Column('failed_attempts', sa.Integer(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('locked_until', sa.DateTime(), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('sent_via', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ['customer_id'], ['customers.id'],
            name='fk_mrac_customer_id', ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['created_by_id'], ['users.id'],
            name='fk_mrac_created_by_id',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'customer_id', 'year', name='uq_mrac_customer_year',
        ),
    )
    op.create_index(
        'ix_meter_reading_access_codes_customer_id',
        'meter_reading_access_codes', ['customer_id'],
    )
    op.create_index(
        'ix_meter_reading_access_codes_year',
        'meter_reading_access_codes', ['year'],
    )
    op.create_index(
        'ix_mrac_year_revoked_expires',
        'meter_reading_access_codes', ['year', 'revoked_at', 'expires_at'],
    )

    # 2. Marker-Spalten an meter_readings haengen — batch_alter_table fuer
    #    SQLite-Vertraeglichkeit (auf Postgres no-op-Wrapper).
    with op.batch_alter_table('meter_readings') as batch_op:
        batch_op.add_column(sa.Column(
            'entered_via_self_service', sa.Boolean(),
            nullable=False, server_default=sa.false(),
        ))
        batch_op.add_column(sa.Column(
            'self_service_code_id', sa.Integer(), nullable=True,
        ))
        batch_op.create_index(
            'ix_meter_readings_entered_via_self_service',
            ['entered_via_self_service'],
        )
        batch_op.create_foreign_key(
            'fk_meter_readings_self_service_code_id',
            'meter_reading_access_codes',
            ['self_service_code_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade():
    with op.batch_alter_table('meter_readings') as batch_op:
        batch_op.drop_constraint(
            'fk_meter_readings_self_service_code_id', type_='foreignkey',
        )
        batch_op.drop_index('ix_meter_readings_entered_via_self_service')
        batch_op.drop_column('self_service_code_id')
        batch_op.drop_column('entered_via_self_service')

    op.drop_index(
        'ix_mrac_year_revoked_expires',
        table_name='meter_reading_access_codes',
    )
    op.drop_index(
        'ix_meter_reading_access_codes_year',
        table_name='meter_reading_access_codes',
    )
    op.drop_index(
        'ix_meter_reading_access_codes_customer_id',
        table_name='meter_reading_access_codes',
    )
    op.drop_table('meter_reading_access_codes')
