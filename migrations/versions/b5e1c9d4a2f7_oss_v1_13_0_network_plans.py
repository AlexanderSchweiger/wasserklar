"""[oss-v1.13.0] add network plans (mehrere Leitungsplaene)

Eltern-Tabelle ``network_plans`` (Name, Status entwurf/aktiv/archiviert,
Wartungs-Schalter, Ersteller/letzter Aenderer, Herkunft ``source_plan_id`` fuer
Kopien). ``network_features`` bekommt ein Pflicht-``plan_id`` (Bestandsdaten
werden auf einen Default-„Hauptplan" gebackfillt) und ``source_feature_id``
(Abstammung einer Kopie fuer den Plan-Merge).

Revision ID: b5e1c9d4a2f7
Revises: a3d7e2f9c1b4
Create Date: 2026-06-04

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b5e1c9d4a2f7'
down_revision = 'a3d7e2f9c1b4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'network_plans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=20), server_default=sa.text("'entwurf'"), nullable=False),
        sa.Column('maintenance_enabled', sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('source_plan_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('updated_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['source_plan_id'], ['network_plans.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )

    # network_features: plan_id (zunaechst nullable fuer den Backfill) +
    # source_feature_id (Self-FK, Abstammung). Indizes + FKs im Batch-Modus,
    # damit es auch auf SQLite (ALTER-Restriktionen) durchlaeuft.
    with op.batch_alter_table('network_features', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plan_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('source_feature_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_network_features_plan_id'), ['plan_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_network_features_source_feature_id'), ['source_feature_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_network_features_plan_id', 'network_plans', ['plan_id'], ['id'],
        )
        batch_op.create_foreign_key(
            'fk_network_features_source_feature_id', 'network_features',
            ['source_feature_id'], ['id'], ondelete='SET NULL',
        )

    # Default-Plan „Hauptplan" (aktiv) anlegen und alle Bestands-Features
    # zuordnen. maintenance_enabled/Timestamps bewusst weggelassen -> der
    # server_default greift, kein dialekt-spezifisches Bool-/Timestamp-Literal.
    op.execute("INSERT INTO network_plans (name, status) VALUES ('Hauptplan', 'aktiv')")
    op.execute(
        "UPDATE network_features SET plan_id = "
        "(SELECT id FROM network_plans ORDER BY id LIMIT 1) WHERE plan_id IS NULL"
    )

    # plan_id ist jetzt befuellt -> NOT NULL erzwingen.
    with op.batch_alter_table('network_features', schema=None) as batch_op:
        batch_op.alter_column('plan_id', existing_type=sa.Integer(), nullable=False)


def downgrade():
    with op.batch_alter_table('network_features', schema=None) as batch_op:
        batch_op.drop_constraint('fk_network_features_source_feature_id', type_='foreignkey')
        batch_op.drop_constraint('fk_network_features_plan_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_network_features_source_feature_id'))
        batch_op.drop_index(batch_op.f('ix_network_features_plan_id'))
        batch_op.drop_column('source_feature_id')
        batch_op.drop_column('plan_id')
    op.drop_table('network_plans')
