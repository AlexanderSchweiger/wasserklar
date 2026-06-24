"""[oss-v1.33.0] add hydrant_share_links table + network_features.hydrant_type

Oeffentlicher Feuerwehr-Freigabe-Link fuer den Hydrantenplan (Zugriff ohne Login
ueber ein hochentropisches Token, einloesende Route im SaaS-Layer) plus die
strukturierte Hydranten-Bauart (Ueber-/Unterflur) am NetworkFeature. Additiv,
dialekt-portabel (SQLite/MariaDB/Postgres).

Revision ID: a7d4f1c9e2b6
Revises: c5e1a9d72f4b
Create Date: 2026-06-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7d4f1c9e2b6'
down_revision = 'c5e1a9d72f4b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'hydrant_share_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=120), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('last_accessed_at', sa.DateTime(), nullable=True),
        sa.Column('access_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['network_plans.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('hydrant_share_links', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_hydrant_share_links_plan_id'),
            ['plan_id'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_hydrant_share_links_token'),
            ['token'], unique=True,
        )

    with op.batch_alter_table('network_features', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hydrant_type', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('network_features', schema=None) as batch_op:
        batch_op.drop_column('hydrant_type')

    with op.batch_alter_table('hydrant_share_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_hydrant_share_links_token'))
        batch_op.drop_index(batch_op.f('ix_hydrant_share_links_plan_id'))
    op.drop_table('hydrant_share_links')
