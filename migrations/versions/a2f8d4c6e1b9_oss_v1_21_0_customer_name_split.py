"""oss-v1.21.0 customer name split

Revision ID: a2f8d4c6e1b9
Revises: f1a7c3e9d2b5
Create Date: 2026-06-07

Aufspaltung des Kundennamens fuer eine korrekte Brief-/Rechnungsanrede:

* ``customers.salutation`` — Anrede: "Herr" | "Frau" | "Familie" | NULL.
* ``customers.first_name`` — Vorname (Person), NULL bei Firmen/Familien.
* ``customers.last_name``  — Nachname (Person/Familie), NULL bei Firmen.
* ``customers.is_company`` — Firmen-Flag (ein Name, keine Anrede).

``customers.name`` bleibt als kombiniertes Sortier-/Listen-/Suchfeld erhalten
(Konvention "Nachname Vorname") und wird kuenftig beim Speichern aus
last_name + first_name abgeleitet. Bestehende Zeilen behalten ihren ``name``;
``letter_name``/``salutation_line`` fallen auf ``name`` zurueck, solange die
Einzelfelder leer sind — der optionale CLI-Befehl ``split-customer-names``
fuellt sie heuristisch vor.

Reine Spalten-Adds: die drei String-Spalten sind nullable, ``is_company`` ist
NOT NULL mit ``server_default=false`` (dialekt-portabel via ``sa.false()`` —
SQLite/MariaDB/Postgres). Bestehende Zeilen bleiben gueltig.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a2f8d4c6e1b9'
down_revision = 'f1a7c3e9d2b5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('customers',
                  sa.Column('salutation', sa.String(length=10), nullable=True))
    op.add_column('customers',
                  sa.Column('first_name', sa.String(length=100), nullable=True))
    op.add_column('customers',
                  sa.Column('last_name', sa.String(length=100), nullable=True))
    op.add_column('customers',
                  sa.Column('is_company', sa.Boolean(), nullable=False,
                            server_default=sa.false()))


def downgrade():
    op.drop_column('customers', 'is_company')
    op.drop_column('customers', 'last_name')
    op.drop_column('customers', 'first_name')
    op.drop_column('customers', 'salutation')
