"""oss-v1.20.0 user invitations

Revision ID: f1a7c3e9d2b5
Revises: c3d9f1a7e2b4
Create Date: 2026-06-06

Benutzer-Einladung per Einladungslink (SaaS-Feature, DB-Spalten im OSS, weil
sie ins Tenant-Schema migriert werden muessen):

* ``users.invited_at``             — Zeitpunkt der (letzten) Einladung.
* ``users.invitation_accepted_at`` — gesetzt, sobald der Eingeladene per Link
  sein Passwort gesetzt und sich aktiviert hat.

"Einladung ausstehend" := invited_at IS NOT NULL AND invitation_accepted_at IS NULL.

Reine Spalten-Adds (beide nullable) — dialekt-portabel (SQLite/MariaDB/Postgres),
bestehende Zeilen bleiben gueltig.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1a7c3e9d2b5'
down_revision = 'c3d9f1a7e2b4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('users',
                  sa.Column('invited_at', sa.DateTime(), nullable=True))
    op.add_column('users',
                  sa.Column('invitation_accepted_at', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('users', 'invitation_accepted_at')
    op.drop_column('users', 'invited_at')
