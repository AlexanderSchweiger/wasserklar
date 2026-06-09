"""[oss-v1.24.0] add email_suppressions table

Pro-Tenant-Sperrliste fuer unzustellbare/abgelehnte E-Mail-Adressen, gekeyt auf
die normalisierte Adresse (unique). Gespeist vom Postmark-/Brevo-Webhook
(Hard-Bounce / Spam-Beschwerde) bzw. vom synchronen SMTP-Permanentfehler im
OSS-Standalone-Betrieb; manuelle Eintraege und Freigaben moeglich. ``active``
ist die Freigabe-Markierung (False = wieder freigegeben). Kein Backfill — die
Sperre speist sich ab jetzt aus neuen Bounce-Events.

Revision ID: b7e2c9f4a1d6
Revises: e9c3a7b1d5f2
Create Date: 2026-06-08

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e2c9f4a1d6'
down_revision = 'e9c3a7b1d5f2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'email_suppressions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('reason', sa.String(length=32), nullable=False),
        sa.Column('detail', sa.String(length=512), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False),
        sa.Column('bounce_count', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('source_subject_type', sa.String(length=32), nullable=True),
        sa.Column('source_subject_id', sa.Integer(), nullable=True),
        sa.Column('active', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email', name='uq_email_suppressions_email'),
    )
    # Index im Batch-Modus (Konvention, laeuft auch auf SQLite durch).
    with op.batch_alter_table('email_suppressions', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_email_suppressions_active'), ['active'], unique=False)


def downgrade():
    with op.batch_alter_table('email_suppressions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_email_suppressions_active'))
    op.drop_table('email_suppressions')
