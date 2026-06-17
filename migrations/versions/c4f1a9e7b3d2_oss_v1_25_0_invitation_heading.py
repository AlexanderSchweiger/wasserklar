"""[oss-v1.25.0] add invitation_heading to meetings

Frei einstellbare Überschrift der Einladung (Default bleibt "Einladung zur
<Sitzungstyp>"; leer = Fallback im Template/DOCX). Kein Backfill — Bestands-
sitzungen behalten NULL und rendern damit weiterhin den Default-Text.

Revision ID: c4f1a9e7b3d2
Revises: b7e2c9f4a1d6
Create Date: 2026-06-17

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4f1a9e7b3d2'
down_revision = 'b7e2c9f4a1d6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('invitation_heading', sa.String(length=200), nullable=True))


def downgrade():
    with op.batch_alter_table('meetings', schema=None) as batch_op:
        batch_op.drop_column('invitation_heading')
