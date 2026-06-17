"""[oss-v1.26.0] add incident journal tables

Stoerungs-/Rohrbruch-Journal: ``incidents`` (Ereignisjournal mit GeoJSON-Point-
Lage als Text, Ursachenkategorie, Status, Schweregrad, operative Kennzahlen
fuer den Jahresbericht — Wasserverlust/Kosten/betroffene Anschluesse — und
optionalen FKs auf Customer/Property/NetworkFeature) plus ``incident_photos``
(Foto-Metadaten; die Datei liegt im instance-Volume, nicht in der DB).

Rein additiv (zwei neue Tabellen) — dialekt-portabel (SQLite/MariaDB/Postgres),
kein PostGIS, kein JSON-Typ. ``server_default`` als einfache String-Literale.

Revision ID: 2afa7062937a
Revises: c4f1a9e7b3d2
Create Date: 2026-06-17

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2afa7062937a'
down_revision = 'c4f1a9e7b3d2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'incidents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('incident_type', sa.String(length=40), server_default=sa.text("'rohrbruch'"), nullable=False),
        sa.Column('severity', sa.String(length=20), server_default=sa.text("'mittel'"), nullable=False),
        sa.Column('status', sa.String(length=20), server_default=sa.text("'offen'"), nullable=False),
        sa.Column('cause', sa.String(length=40), nullable=True),
        sa.Column('detected_at', sa.Date(), nullable=False),
        sa.Column('resolved_at', sa.Date(), nullable=True),
        sa.Column('location_geojson', sa.Text(), nullable=True),
        sa.Column('lat', sa.Float(), nullable=True),
        sa.Column('lng', sa.Float(), nullable=True),
        sa.Column('location_description', sa.String(length=255), nullable=True),
        sa.Column('water_loss_m3', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('affected_count', sa.Integer(), nullable=True),
        sa.Column('cost', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('performed_by', sa.String(length=120), nullable=True),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('repair_notes', sa.Text(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('property_id', sa.Integer(), nullable=True),
        sa.Column('feature_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['feature_id'], ['network_features.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['property_id'], ['properties.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incidents_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_incidents_feature_id'), ['feature_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_incidents_property_id'), ['property_id'], unique=False)

    op.create_table(
        'incident_photos',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('incident_id', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('original_name', sa.String(length=255), nullable=True),
        sa.Column('content_type', sa.String(length=80), nullable=True),
        sa.Column('caption', sa.String(length=255), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), nullable=True),
        sa.Column('uploaded_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ),
        sa.ForeignKeyConstraint(['uploaded_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('incident_photos', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incident_photos_incident_id'), ['incident_id'], unique=False)


def downgrade():
    with op.batch_alter_table('incident_photos', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_incident_photos_incident_id'))
    op.drop_table('incident_photos')

    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_incidents_property_id'))
        batch_op.drop_index(batch_op.f('ix_incidents_feature_id'))
        batch_op.drop_index(batch_op.f('ix_incidents_customer_id'))
    op.drop_table('incidents')
