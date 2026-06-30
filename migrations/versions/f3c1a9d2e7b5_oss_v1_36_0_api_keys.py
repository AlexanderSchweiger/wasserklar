"""[oss-v1.36.0] add api_keys table

Pro-API-/MCP-Schluessel pro Tenant, im Tenant-Schema. Im OSS-Standalone
ungenutzt (analog ``async_jobs`` / ``admin_notifications``); die versionierte
REST-API (/api/v1) und der MCP-Server liegen im SaaS-Layer und sind nur fuer den
Pro-Plan freigeschaltet. Additiv und dialekt-portabel (SQLite/MariaDB/Postgres):
nur eine neue Tabelle, gespeichert wird ausschliesslich der SHA-256-Hash,
``server_default`` als ``sa.text(...)``, ``created_at`` per ``CURRENT_TIMESTAMP``
(in allen drei Dialekten gueltig), Indices via ``batch_op.f(...)``.

Revision ID: f3c1a9d2e7b5
Revises: e1a9c7f3b5d2
Create Date: 2026-06-30 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3c1a9d2e7b5'
down_revision = 'e1a9c7f3b5d2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'api_keys',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=False),
        sa.Column('key_prefix', sa.String(length=64), nullable=False),
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('scopes', sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column('mcp_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_keys_key_hash'), ['key_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_api_keys_created_by_id'), ['created_by_id'], unique=False)


def downgrade():
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_keys_created_by_id'))
        batch_op.drop_index(batch_op.f('ix_api_keys_key_hash'))
    op.drop_table('api_keys')
