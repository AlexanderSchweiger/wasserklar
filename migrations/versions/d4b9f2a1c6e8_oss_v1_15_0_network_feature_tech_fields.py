"""[oss-v1.15.0] add manufacturer/depth/ground-level/pressure to network_features

Vier Fachfelder am NetworkFeature: ``manufacturer`` (Fabrikat),
``installation_depth_m`` (Einbautiefe), ``ground_level_m`` (GOK-Hoehe,
Gelaendeoberkante) und ``pressure_rating`` (Druckstufe, z. B. "PN 10").
Werden beim WLK-Import optional aus der Notiz extrahiert (siehe
services.parse_note_fields). Alle nullable -> kein Backfill noetig.

Revision ID: d4b9f2a1c6e8
Revises: c2f4a8d1e9b3
Create Date: 2026-06-04

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4b9f2a1c6e8'
down_revision = 'c2f4a8d1e9b3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('network_features', schema=None) as batch_op:
        batch_op.add_column(sa.Column('manufacturer', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('installation_depth_m', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('ground_level_m', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('pressure_rating', sa.String(length=20), nullable=True))


def downgrade():
    with op.batch_alter_table('network_features', schema=None) as batch_op:
        batch_op.drop_column('pressure_rating')
        batch_op.drop_column('ground_level_m')
        batch_op.drop_column('installation_depth_m')
        batch_op.drop_column('manufacturer')
