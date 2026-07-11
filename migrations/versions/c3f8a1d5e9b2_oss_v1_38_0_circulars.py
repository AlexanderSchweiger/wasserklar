"""[oss-v1.38.0] add circulars + circular_recipients + circular_delivery_logs

Rundschreiben & Notfall-Kommunikation: Abkochempfehlung (an den Wasserproben-
Alarm angebunden), Wasserabschaltungs-Infos (Rohrbruch/geplante Reparatur) und
freie Rundschreiben. Versand per E-Mail oder Post (Sammel-PDF), analog den
Massenrechnungen/Sitzungseinladungen. ``circulars`` haelt Kopf + Plaintext-Body,
``circular_recipients`` je Empfaenger die Versandart + E-Mail-Tracking-Spalten
(EmailTrackableMixin), ``circular_delivery_logs`` die Zustell-History.

Additiv und dialekt-portabel (SQLite/MariaDB/Postgres): nur drei neue Tabellen,
``server_default`` als ``sa.text(...)``, Indices via ``batch_op.f(...)``. Der
Self-FK ``predecessor_id`` (Entwarnung → Abkochempfehlung) und die FKs auf
``water_samples``/``incidents`` sind ``ondelete='SET NULL'``.

Revision ID: c3f8a1d5e9b2
Revises: a9e4c1f7b3d6
Create Date: 2026-07-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3f8a1d5e9b2'
down_revision = 'a9e4c1f7b3d6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'circulars',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('subject', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20),
                  server_default=sa.text("'draft'"), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('water_sample_id', sa.Integer(), nullable=True),
        sa.Column('incident_id', sa.Integer(), nullable=True),
        sa.Column('predecessor_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['water_sample_id'], ['water_samples.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['predecessor_id'], ['circulars.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('circulars', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_circulars_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_circulars_water_sample_id'),
                              ['water_sample_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_circulars_incident_id'),
                              ['incident_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_circulars_predecessor_id'),
                              ['predecessor_id'], unique=False)

    op.create_table(
        'circular_recipients',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('circular_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('delivery_method', sa.String(length=10), nullable=True),
        sa.Column('post_sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        # EmailTrackableMixin-Spalten.
        sa.Column('email_message_id', sa.String(length=128), nullable=True),
        sa.Column('email_sent_at', sa.DateTime(), nullable=True),
        sa.Column('email_recipient', sa.String(length=255), nullable=True),
        sa.Column('last_email_status', sa.String(length=32), nullable=True),
        sa.Column('last_email_status_at', sa.DateTime(), nullable=True),
        sa.Column('last_email_bounce_detail', sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(['circular_id'], ['circulars.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('circular_id', 'customer_id', name='uq_circular_recipient'),
    )
    with op.batch_alter_table('circular_recipients', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_circular_recipients_circular_id'),
                              ['circular_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_circular_recipients_customer_id'),
                              ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_circular_recipients_email_message_id'),
                              ['email_message_id'], unique=False)

    op.create_table(
        'circular_delivery_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('circular_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('recipient_name', sa.String(length=200), nullable=True),
        sa.Column('recipient_email', sa.String(length=255), nullable=True),
        sa.Column('method', sa.String(length=10), nullable=False),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(['circular_id'], ['circulars.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('circular_delivery_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_circular_delivery_logs_circular_id'),
                              ['circular_id'], unique=False)


def downgrade():
    with op.batch_alter_table('circular_delivery_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_circular_delivery_logs_circular_id'))
    op.drop_table('circular_delivery_logs')

    with op.batch_alter_table('circular_recipients', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_circular_recipients_email_message_id'))
        batch_op.drop_index(batch_op.f('ix_circular_recipients_customer_id'))
        batch_op.drop_index(batch_op.f('ix_circular_recipients_circular_id'))
    op.drop_table('circular_recipients')

    with op.batch_alter_table('circulars', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_circulars_predecessor_id'))
        batch_op.drop_index(batch_op.f('ix_circulars_incident_id'))
        batch_op.drop_index(batch_op.f('ix_circulars_water_sample_id'))
        batch_op.drop_index(batch_op.f('ix_circulars_kind'))
    op.drop_table('circulars')
