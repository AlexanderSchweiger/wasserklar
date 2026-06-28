"""[oss-v1.34.0] add water_samples + lab_results tables

Wasserproben / TWV-Beprobung: ein Laborbefund (``water_samples``) je Entnahme an
einer ``network_features``-Probenahmestelle (feature_type='probenahme') buendelt
mehrere Laborwerte (``lab_results``: Parameter, Wert, Einheit, Grenzwert-Snapshot,
Ampel-Status). Geschwister-Pattern zu ``spring_yields``. Additiv, dialekt-portabel
(SQLite/MariaDB/Postgres): nur neue Tabellen, ``sa.Numeric`` statt Float,
``server_default`` als ``sa.text("'ok'")``, Indices via ``batch_op.f(...)``.

Revision ID: b8f3c2d9e7a1
Revises: a7d4f1c9e2b6
Create Date: 2026-06-28 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8f3c2d9e7a1'
down_revision = 'a7d4f1c9e2b6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'water_samples',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('feature_id', sa.Integer(), nullable=False),
        sa.Column('sample_date', sa.Date(), nullable=False),
        sa.Column('lab_name', sa.String(length=160), nullable=True),
        sa.Column('sample_no', sa.String(length=80), nullable=True),
        sa.Column('sample_type', sa.String(length=40), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['feature_id'], ['network_features.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('water_samples', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_water_samples_feature_id'),
            ['feature_id'], unique=False,
        )

    op.create_table(
        'lab_results',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('water_sample_id', sa.Integer(), nullable=False),
        sa.Column('parameter_key', sa.String(length=40), nullable=False),
        sa.Column('value_num', sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column('value_text', sa.String(length=80), nullable=True),
        sa.Column('unit', sa.String(length=20), nullable=True),
        sa.Column('limit_text', sa.String(length=60), nullable=True),
        sa.Column('status', sa.String(length=12), server_default=sa.text("'ok'"), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['water_sample_id'], ['water_samples.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('lab_results', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_lab_results_water_sample_id'),
            ['water_sample_id'], unique=False,
        )


def downgrade():
    with op.batch_alter_table('lab_results', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_lab_results_water_sample_id'))
    op.drop_table('lab_results')
    with op.batch_alter_table('water_samples', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_water_samples_feature_id'))
    op.drop_table('water_samples')
