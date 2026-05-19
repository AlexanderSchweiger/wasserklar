"""[oss-v1.4.0] Preis pro m³ mit 4 Nachkommastellen

Revision ID: c7e1f9a2b3d4
Revises: f4a2b8c1d3e5
Create Date: 2026-05-19 10:00:00.000000

Erweitert die Tarif-Preisspalten von ``Numeric(10, 2)`` auf ``Numeric(10, 4)``,
damit der Wasserpreis pro m³ mit vier Nachkommastellen erfasst werden kann:

  - ``water_tariffs.price_per_m3``
  - ``billing_runs.tariff_price_per_m3`` (Tarif-Snapshot des Rechnungslaufs)

Schema-Aenderung ueber ``batch_alter_table`` (SQLite-Pflicht fuer ALTER COLUMN);
auf MariaDB/Postgres ein normales ALTER TABLE. Reine Praezisionserweiterung,
keine Datenmigration noetig. ``downgrade`` ist verlustbehaftet — vorhandene
3./4. Nachkommastellen werden auf zwei gerundet.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7e1f9a2b3d4'
down_revision = 'f4a2b8c1d3e5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('water_tariffs') as batch_op:
        batch_op.alter_column(
            'price_per_m3',
            existing_type=sa.Numeric(10, 2),
            type_=sa.Numeric(10, 4),
            existing_nullable=False,
        )
    with op.batch_alter_table('billing_runs') as batch_op:
        batch_op.alter_column(
            'tariff_price_per_m3',
            existing_type=sa.Numeric(10, 2),
            type_=sa.Numeric(10, 4),
            existing_nullable=False,
        )


def downgrade():
    with op.batch_alter_table('billing_runs') as batch_op:
        batch_op.alter_column(
            'tariff_price_per_m3',
            existing_type=sa.Numeric(10, 4),
            type_=sa.Numeric(10, 2),
            existing_nullable=False,
        )
    with op.batch_alter_table('water_tariffs') as batch_op:
        batch_op.alter_column(
            'price_per_m3',
            existing_type=sa.Numeric(10, 4),
            type_=sa.Numeric(10, 2),
            existing_nullable=False,
        )
