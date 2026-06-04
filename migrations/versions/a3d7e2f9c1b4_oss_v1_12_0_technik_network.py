"""[oss-v1.12.0] add technik network features, maintenance logs, feature photos

Wasserleitungsplan-Modul (Menue „Technik"): NetworkFeature (Punkt/Linie als
GeoJSON-in-Text, dialekt-portabel, kein PostGIS), MaintenanceLog
(Wartungs-/Pruefprotokoll mit next_due fuer die Dashboard-Erinnerung) und
FeaturePhoto (Foto-Metadaten; die Datei selbst liegt im instance-Volume).

Revision ID: a3d7e2f9c1b4
Revises: f2a8c4e6b1d3
Create Date: 2026-06-03

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a3d7e2f9c1b4'
down_revision = 'f2a8c4e6b1d3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'network_features',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('geometry_kind', sa.String(length=10), nullable=False),
        sa.Column('feature_type', sa.String(length=40), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=True),
        sa.Column('geometry', sa.Text(), nullable=False),
        sa.Column('lat', sa.Float(), nullable=True),
        sa.Column('lng', sa.Float(), nullable=True),
        sa.Column('length_m', sa.Float(), nullable=True),
        sa.Column('accuracy', sa.String(length=20), server_default=sa.text("'geschaetzt'"), nullable=False),
        sa.Column('material', sa.String(length=60), nullable=True),
        sa.Column('dimension_dn', sa.Integer(), nullable=True),
        sa.Column('year_built', sa.Integer(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('property_id', sa.Integer(), nullable=True),
        sa.Column('meter_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['meter_id'], ['water_meters.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('network_features', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_network_features_property_id'), ['property_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_network_features_meter_id'), ['meter_id'], unique=False)

    op.create_table(
        'maintenance_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('feature_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('kind', sa.String(length=30), server_default=sa.text("'inspektion'"), nullable=False),
        sa.Column('result', sa.String(length=20), nullable=True),
        sa.Column('next_due', sa.Date(), nullable=True),
        sa.Column('interval_months', sa.Integer(), nullable=True),
        sa.Column('performed_by', sa.String(length=120), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['feature_id'], ['network_features.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('maintenance_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_maintenance_logs_feature_id'), ['feature_id'], unique=False)

    op.create_table(
        'feature_photos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('feature_id', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('original_name', sa.String(length=255), nullable=True),
        sa.Column('content_type', sa.String(length=80), nullable=True),
        sa.Column('caption', sa.String(length=255), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), nullable=True),
        sa.Column('uploaded_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['feature_id'], ['network_features.id']),
        sa.ForeignKeyConstraint(['uploaded_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('feature_photos', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_feature_photos_feature_id'), ['feature_id'], unique=False)


def downgrade():
    op.drop_table('feature_photos')
    op.drop_table('maintenance_logs')
    with op.batch_alter_table('network_features', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_network_features_meter_id'))
        batch_op.drop_index(batch_op.f('ix_network_features_property_id'))
    op.drop_table('network_features')
