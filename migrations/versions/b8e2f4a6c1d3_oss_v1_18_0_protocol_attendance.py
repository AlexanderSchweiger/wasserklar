"""oss-v1.18.0 protocol attendance (Freitext, Kopfzahl, Wartefrist/Wiedereroeffnung)

Revision ID: b8e2f4a6c1d3
Revises: e7b3a1f95c24
Create Date: 2026-06-05

Erweitert ``meeting_protocols`` um die Anwesenheits-Erfassung: Freitext-Modus
statt Personenliste (``attendance_mode`` / ``attendance_freetext``), eine manuell
erfasste Kopfzahl (``present_headcount``) sowie die Wiedereroeffnung der
Hauptversammlung nach erfolgloser Wartefrist (``reconvened`` /
``reconvene_wait_minutes``). Reine Spalten-Adds mit server_default —
dialekt-portabel (SQLite/MariaDB/Postgres), bestehende Zeilen bleiben gueltig.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8e2f4a6c1d3'
down_revision = 'e7b3a1f95c24'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('meeting_protocols',
                  sa.Column('attendance_mode', sa.String(length=10),
                            server_default=sa.text("'list'"), nullable=False))
    op.add_column('meeting_protocols',
                  sa.Column('attendance_freetext', sa.Text(), nullable=True))
    op.add_column('meeting_protocols',
                  sa.Column('present_headcount', sa.Integer(), nullable=True))
    op.add_column('meeting_protocols',
                  sa.Column('reconvened', sa.Boolean(),
                            server_default=sa.false(), nullable=False))
    op.add_column('meeting_protocols',
                  sa.Column('reconvene_wait_minutes', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('meeting_protocols', 'reconvene_wait_minutes')
    op.drop_column('meeting_protocols', 'reconvened')
    op.drop_column('meeting_protocols', 'present_headcount')
    op.drop_column('meeting_protocols', 'attendance_freetext')
    op.drop_column('meeting_protocols', 'attendance_mode')
