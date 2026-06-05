"""[oss-v1.14.0] rename permission_key 'technik' to 'network'

Das Leitungsnetz-Modul wurde von /technik auf /network umbenannt.
Das Recht "technik" in role_permissions wird auf "network" migriert,
damit bestehende Rollen-Zuweisungen erhalten bleiben.

Revision ID: c2f4a8d1e9b3
Revises: b5e1c9d4a2f7
Create Date: 2026-06-04

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c2f4a8d1e9b3'
down_revision = 'b5e1c9d4a2f7'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text(
            "UPDATE role_permissions SET permission_key = 'network'"
            " WHERE permission_key = 'technik'"
        )
    )


def downgrade():
    op.execute(
        sa.text(
            "UPDATE role_permissions SET permission_key = 'technik'"
            " WHERE permission_key = 'network'"
        )
    )
