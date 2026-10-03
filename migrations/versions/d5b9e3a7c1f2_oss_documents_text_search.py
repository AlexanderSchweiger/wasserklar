"""Belegablage Stufe 2: Belegtext (Volltextsuche) und Kennzeichen „automatisch erkannt"

``documents`` bekommt drei Spalten:

* ``text_content`` — der Text der PDF-Textebene (``app/documents/extract.py``), Grundlage der
  Volltextsuche. Abgeleitet, nie Teil des Belegs; die Datei bleibt Byte fuer Byte. Auf
  MySQL/MariaDB ``MEDIUMTEXT`` (``TEXT`` fasst nur 64 KB).
* ``text_status`` — ``ok`` | ``empty`` (PDF ohne Textebene, z. B. ein Scan) | ``error``;
  NULL = noch nicht gelesen (Bestand, Fotos, E-Rechnungs-XML).
* ``meta_auto`` — die Angaben (Titel/Nummer/Datum/Betrag/Lieferant) stammen aus der
  automatischen Erkennung und sind noch nicht bestaetigt.

Bestehende Belege bleiben unveraendert (NULL / false); ``flask documents-index-text`` liest den
Text nach. Dialekt-portabel: ``batch_alter_table`` (SQLite), ``server_default`` fuer die
NOT-NULL-Spalte.

Revision ID: d5b9e3a7c1f2
Revises: c4f8a2d6b1e9
Create Date: 2026-10-03 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


# revision identifiers, used by Alembic.
revision = 'd5b9e3a7c1f2'
down_revision = 'c4f8a2d6b1e9'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'text_content', sa.Text().with_variant(mysql.MEDIUMTEXT(), 'mysql', 'mariadb'), nullable=True))
        batch_op.add_column(sa.Column('text_status', sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column(
            'meta_auto', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_column('meta_auto')
        batch_op.drop_column('text_status')
        batch_op.drop_column('text_content')
