"""[oss-v1.32.0] add spring_yields table

Quellschuettungs-Messreihe (Schuettung in l/s je Quelle) fuer das Trockenheits-
Monitoring. Haengt an einer ``network_features``-Quelle (feature_type='quelle'),
Geschwister-Pattern zu ``maintenance_logs``. Additiv, dialekt-portabel.

Revision ID: c5e1a9d72f4b
Revises: 8097945ec625
Create Date: 2026-06-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c5e1a9d72f4b'
down_revision = '8097945ec625'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'spring_yields',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('feature_id', sa.Integer(), nullable=False),
        sa.Column('measurement_date', sa.Date(), nullable=False),
        sa.Column('flow_rate_lps', sa.Numeric(precision=8, scale=3), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['feature_id'], ['network_features.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('spring_yields', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_spring_yields_feature_id'),
            ['feature_id'], unique=False,
        )


def downgrade():
    with op.batch_alter_table('spring_yields', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_spring_yields_feature_id'))
    op.drop_table('spring_yields')
