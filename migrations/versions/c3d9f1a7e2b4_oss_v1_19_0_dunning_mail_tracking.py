"""oss-v1.19.0 dunning mail tracking + stage letter texts

Revision ID: c3d9f1a7e2b4
Revises: b8e2f4a6c1d3
Create Date: 2026-06-05

Mahnwesen-Ausbau:

* ``dunning_notices`` bekommt die sechs ``EmailTrackableMixin``-Spalten, damit
  Mahnungen denselben E-Mail-Versand-/Zustell-/Bounce-Audit-Trail wie Rechnungen
  fuehren (Postmark-Webhook schreibt ueber ``subject_type='dunning'`` zurueck).
* ``dunning_stages`` bekommt ``letter_intro`` / ``letter_closing`` fuer den
  pro-Stufe konfigurierbaren Brieftext (``email_subject`` / ``email_body``
  existieren bereits seit dem Initial-Schema).

Reine Spalten-Adds (alle nullable) — dialekt-portabel (SQLite/MariaDB/Postgres),
bestehende Zeilen bleiben gueltig.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d9f1a7e2b4'
down_revision = 'b8e2f4a6c1d3'
branch_labels = None
depends_on = None


def upgrade():
    # Pro-Stufe-Brieftexte
    op.add_column('dunning_stages',
                  sa.Column('letter_intro', sa.Text(), nullable=True))
    op.add_column('dunning_stages',
                  sa.Column('letter_closing', sa.Text(), nullable=True))

    # EmailTrackableMixin-Spalten auf den Mahnungen
    op.add_column('dunning_notices',
                  sa.Column('email_message_id', sa.String(length=128), nullable=True))
    op.add_column('dunning_notices',
                  sa.Column('email_sent_at', sa.DateTime(), nullable=True))
    op.add_column('dunning_notices',
                  sa.Column('email_recipient', sa.String(length=255), nullable=True))
    op.add_column('dunning_notices',
                  sa.Column('last_email_status', sa.String(length=32), nullable=True))
    op.add_column('dunning_notices',
                  sa.Column('last_email_status_at', sa.DateTime(), nullable=True))
    op.add_column('dunning_notices',
                  sa.Column('last_email_bounce_detail', sa.String(length=512), nullable=True))
    op.create_index('ix_dunning_notices_email_message_id', 'dunning_notices',
                    ['email_message_id'], unique=False)


def downgrade():
    op.drop_index('ix_dunning_notices_email_message_id', table_name='dunning_notices')
    op.drop_column('dunning_notices', 'last_email_bounce_detail')
    op.drop_column('dunning_notices', 'last_email_status_at')
    op.drop_column('dunning_notices', 'last_email_status')
    op.drop_column('dunning_notices', 'email_recipient')
    op.drop_column('dunning_notices', 'email_sent_at')
    op.drop_column('dunning_notices', 'email_message_id')
    op.drop_column('dunning_stages', 'letter_closing')
    op.drop_column('dunning_stages', 'letter_intro')
