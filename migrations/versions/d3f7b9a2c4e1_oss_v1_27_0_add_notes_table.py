"""[oss-v1.27.0] add notes table

Notizzettel / Pin-Notizen: eine polymorphe ``notes``-Tabelle (``entity_type``
String-Diskriminator + nullable ``entity_id``, KEIN DB-FK auf entity_id →
dialekt-portabel und von den Zieltabellen entkoppelt). ``entity_type='tenant'``
⇒ ``entity_id`` IS NULL. ``created_by_id`` referenziert ``users`` (ON DELETE
SET NULL). Komposit-Index ``(entity_type, entity_id)`` fuer die N+1-freie
Zeilen-Abfrage.

Rein additiv (eine neue Tabelle) — dialekt-portabel (SQLite/MariaDB/Postgres),
kein PostGIS, kein JSON-Typ.

Revision ID: d3f7b9a2c4e1
Revises: 2afa7062937a
Create Date: 2026-06-17

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd3f7b9a2c4e1'
down_revision = '2afa7062937a'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'notes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entity_type', sa.String(length=32), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('color', sa.String(length=16), server_default=sa.text("'yellow'"), nullable=False),
        sa.Column('pinned', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('notes', schema=None) as batch_op:
        batch_op.create_index('ix_notes_entity', ['entity_type', 'entity_id'], unique=False)


def downgrade():
    with op.batch_alter_table('notes', schema=None) as batch_op:
        batch_op.drop_index('ix_notes_entity')
    op.drop_table('notes')
