"""oss-v1.17.0 schriftfuehrung (Sitzungen, Einladungen, Protokolle, Beschluesse, Schriftverkehr)

Revision ID: e7b3a1f95c24
Revises: d728924b030a
Create Date: 2026-06-05

Legt die Tabellen des Schriftfuehrungs-Moduls an (Mandant-Typ
Wassergenossenschaft): Sitzungen (Vorstand + Hauptversammlung, gemeinsame
Tabelle via meeting_type), Tagesordnung, Einladungen (E-Mail-Tracking),
Versand-History, Anwesenheit, Beschluesse (eigenes Register) und Protokolle
sowie das eigenstaendige Schriftverkehr-Archiv. Reine CREATE TABLEs —
dialekt-portabel (SQLite/MariaDB/Postgres), keine Aenderung an Bestandstabellen.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e7b3a1f95c24'
down_revision = 'd728924b030a'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'meetings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_type', sa.String(length=20), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('meeting_date', sa.Date(), nullable=True),
        sa.Column('start_time', sa.Time(), nullable=True),
        sa.Column('end_time', sa.Time(), nullable=True),
        sa.Column('location', sa.String(length=200), nullable=True),
        sa.Column('intro_text', sa.Text(), nullable=True),
        sa.Column('closing_text', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), server_default=sa.text("'planning'"), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_meetings_meeting_type', 'meetings', ['meeting_type'], unique=False)

    op.create_table(
        'meeting_agenda_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('requires_vote', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_meeting_agenda_items_meeting_id', 'meeting_agenda_items', ['meeting_id'], unique=False)

    op.create_table(
        'meeting_invitations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('delivery_method', sa.String(length=10), nullable=True),
        sa.Column('post_sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('email_message_id', sa.String(length=128), nullable=True),
        sa.Column('email_sent_at', sa.DateTime(), nullable=True),
        sa.Column('email_recipient', sa.String(length=255), nullable=True),
        sa.Column('last_email_status', sa.String(length=32), nullable=True),
        sa.Column('last_email_status_at', sa.DateTime(), nullable=True),
        sa.Column('last_email_bounce_detail', sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('meeting_id', 'customer_id', name='uq_meeting_invitation'),
    )
    op.create_index('ix_meeting_invitations_meeting_id', 'meeting_invitations', ['meeting_id'], unique=False)
    op.create_index('ix_meeting_invitations_customer_id', 'meeting_invitations', ['customer_id'], unique=False)
    op.create_index('ix_meeting_invitations_email_message_id', 'meeting_invitations', ['email_message_id'], unique=False)

    op.create_table(
        'meeting_delivery_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('recipient_name', sa.String(length=200), nullable=True),
        sa.Column('recipient_email', sa.String(length=255), nullable=True),
        sa.Column('method', sa.String(length=10), nullable=False),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_meeting_delivery_logs_meeting_id', 'meeting_delivery_logs', ['meeting_id'], unique=False)

    op.create_table(
        'meeting_attendances',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), server_default=sa.text("'present'"), nullable=False),
        sa.Column('is_member', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('weight', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('note', sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('meeting_id', 'customer_id', name='uq_meeting_attendance'),
    )
    op.create_index('ix_meeting_attendances_meeting_id', 'meeting_attendances', ['meeting_id'], unique=False)
    op.create_index('ix_meeting_attendances_customer_id', 'meeting_attendances', ['customer_id'], unique=False)

    op.create_table(
        'meeting_resolutions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('agenda_item_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('status', sa.String(length=20), server_default=sa.text("'accepted'"), nullable=False),
        sa.Column('votes_for', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('votes_against', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('votes_abstain', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('decided_on', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['agenda_item_id'], ['meeting_agenda_items.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_meeting_resolutions_meeting_id', 'meeting_resolutions', ['meeting_id'], unique=False)

    op.create_table(
        'meeting_protocols',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('meeting_id', sa.Integer(), nullable=False),
        sa.Column('source_type', sa.String(length=10), server_default=sa.text("'richtext'"), nullable=False),
        sa.Column('content_html', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), server_default=sa.text("'draft'"), nullable=False),
        sa.Column('quorum_present', sa.Integer(), nullable=True),
        sa.Column('quorum_total', sa.Integer(), nullable=True),
        sa.Column('is_quorate', sa.Boolean(), nullable=True),
        sa.Column('file_path', sa.String(length=500), nullable=True),
        sa.Column('original_filename', sa.String(length=255), nullable=True),
        sa.Column('mime_type', sa.String(length=120), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('finalized_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['meeting_id'], ['meetings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_meeting_protocols_meeting_id', 'meeting_protocols', ['meeting_id'], unique=True)

    op.create_table(
        'schriftverkehr_documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('doc_type', sa.String(length=20), server_default=sa.text("'outgoing'"), nullable=False),
        sa.Column('document_date', sa.Date(), nullable=True),
        sa.Column('file_path', sa.String(length=500), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=True),
        sa.Column('mime_type', sa.String(length=120), nullable=True),
        sa.Column('file_size', sa.Integer(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_schriftverkehr_documents_year', 'schriftverkehr_documents', ['year'], unique=False)


def downgrade():
    op.drop_index('ix_schriftverkehr_documents_year', table_name='schriftverkehr_documents')
    op.drop_table('schriftverkehr_documents')
    op.drop_index('ix_meeting_protocols_meeting_id', table_name='meeting_protocols')
    op.drop_table('meeting_protocols')
    op.drop_index('ix_meeting_resolutions_meeting_id', table_name='meeting_resolutions')
    op.drop_table('meeting_resolutions')
    op.drop_index('ix_meeting_attendances_customer_id', table_name='meeting_attendances')
    op.drop_index('ix_meeting_attendances_meeting_id', table_name='meeting_attendances')
    op.drop_table('meeting_attendances')
    op.drop_index('ix_meeting_delivery_logs_meeting_id', table_name='meeting_delivery_logs')
    op.drop_table('meeting_delivery_logs')
    op.drop_index('ix_meeting_invitations_email_message_id', table_name='meeting_invitations')
    op.drop_index('ix_meeting_invitations_customer_id', table_name='meeting_invitations')
    op.drop_index('ix_meeting_invitations_meeting_id', table_name='meeting_invitations')
    op.drop_table('meeting_invitations')
    op.drop_index('ix_meeting_agenda_items_meeting_id', table_name='meeting_agenda_items')
    op.drop_table('meeting_agenda_items')
    op.drop_index('ix_meetings_meeting_type', table_name='meetings')
    op.drop_table('meetings')
