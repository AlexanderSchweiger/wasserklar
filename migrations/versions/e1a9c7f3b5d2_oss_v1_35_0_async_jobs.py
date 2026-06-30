"""[oss-v1.35.0] add async_jobs table

Generische Hintergrund-Job-Tabelle (Queue + Historie + Ergebnis-Metadaten),
im Tenant-Schema. Im OSS-Standalone ungenutzt (analog ``admin_notifications``);
die SaaS-Schicht befuellt/verarbeitet sie ueber einen Worker-Sidecar. Additiv,
dialekt-portabel (SQLite/MariaDB/Postgres): nur eine neue Tabelle, Status als
String, ``server_default`` als ``sa.text(...)``, ``created_at`` per
``CURRENT_TIMESTAMP`` (in allen drei Dialekten gueltig), Indices via
``batch_op.f(...)``.

Revision ID: e1a9c7f3b5d2
Revises: b8f3c2d9e7a1
Create Date: 2026-06-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e1a9c7f3b5d2'
down_revision = 'b8f3c2d9e7a1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'async_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=40), nullable=False),
        sa.Column('status', sa.String(length=20), server_default=sa.text("'queued'"), nullable=False),
        sa.Column('params', sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column('progress', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('total', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('result_path', sa.String(length=500), nullable=True),
        sa.Column('result_name', sa.String(length=255), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('async_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_async_jobs_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_async_jobs_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_async_jobs_created_by_id'), ['created_by_id'], unique=False)


def downgrade():
    with op.batch_alter_table('async_jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_async_jobs_created_by_id'))
        batch_op.drop_index(batch_op.f('ix_async_jobs_created_at'))
        batch_op.drop_index(batch_op.f('ix_async_jobs_status'))
    op.drop_table('async_jobs')
