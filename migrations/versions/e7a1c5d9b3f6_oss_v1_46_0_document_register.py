"""Dokumentenregister: Ausgangsrechnungen, Mahnungen, Protokolle und Schriftverkehr als Document

Die Belegablage (``documents``) wird das gemeinsame Register aller aufbewahrungspflichtigen
Dateien. Die neuen Bereiche (``documents.area``: ``invoices``, ``dunning``, ``records``) und Arten
brauchen keine neue Spalte; neu sind nur die Bezuege:

* ``document_links.invoice_id`` / ``dunning_notice_id`` / ``meeting_id`` — eine Verknuepfung hat
  weiterhin genau **einen** Bezug (der Service erzwingt das), benannte FKs mit CASCADE und ein
  Unique je Paar wie bei Buchung/Sammelbuchung.
* ``schriftverkehr_documents.document_id`` — 1:1 zum Register (wie ``incoming_invoices``).
* ``feature_photos.size_bytes`` / ``incident_photos.size_bytes`` — die Fotos zaehlen in der
  Speicherstatistik und im Kontingent mit (NULL = Bestand, ``documents-register-files`` misst nach).

Bestehende Dateien werden **nicht** angefasst; ``flask documents-register-files`` traegt die
vorhandenen Rechnungs-/Mahnungs-/Protokoll-/Schriftverkehrsdateien nach (die Datei bleibt liegen,
das Document zeigt mit einem Altschluessel auf sie).

Revision ID: e7a1c5d9b3f6
Revises: d5b9e3a7c1f2
Create Date: 2026-10-03 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e7a1c5d9b3f6'
down_revision = 'd5b9e3a7c1f2'
branch_labels = None
depends_on = None


_LINK_TARGETS = (
    ('invoice_id', 'invoices', 'invoice'),
    ('dunning_notice_id', 'dunning_notices', 'dunning_notice'),
    ('meeting_id', 'meetings', 'meeting'),
)


def upgrade():
    with op.batch_alter_table('document_links', schema=None) as batch_op:
        for column, table, suffix in _LINK_TARGETS:
            batch_op.add_column(sa.Column(column, sa.Integer(), nullable=True))
            batch_op.create_foreign_key(f'fk_document_links_{column}', table, [column], ['id'],
                                        ondelete='CASCADE')
            batch_op.create_unique_constraint(f'uq_document_links_{suffix}', ['document_id', column])
            batch_op.create_index(batch_op.f(f'ix_document_links_{column}'), [column], unique=False)

    with op.batch_alter_table('schriftverkehr_documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('document_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_schriftverkehr_documents_document_id', 'documents',
                                    ['document_id'], ['id'], ondelete='SET NULL')
        batch_op.create_unique_constraint('uq_schriftverkehr_documents_document_id', ['document_id'])

    for table in ('feature_photos', 'incident_photos'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column('size_bytes', sa.BigInteger(), nullable=True))


def downgrade():
    """Best effort: die Register-Eintraege der neuen Bereiche (``documents`` mit area ``invoices``,
    ``dunning``, ``records``) bleiben stehen — die Dateien gehoeren zu den Rechnungen/Mahnungen/
    Protokollen und duerfen beim Zurueckrollen nicht verloren gehen."""
    for table in ('incident_photos', 'feature_photos'):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column('size_bytes')

    with op.batch_alter_table('schriftverkehr_documents', schema=None) as batch_op:
        batch_op.drop_constraint('uq_schriftverkehr_documents_document_id', type_='unique')
        batch_op.drop_constraint('fk_schriftverkehr_documents_document_id', type_='foreignkey')
        batch_op.drop_column('document_id')

    # Verknuepfungen zu den neuen Bezuegen haben ohne die Spalten keinen Elternteil mehr.
    links = sa.table('document_links', sa.column('invoice_id'), sa.column('dunning_notice_id'),
                     sa.column('meeting_id'))
    op.execute(links.delete().where(sa.or_(links.c.invoice_id.isnot(None),
                                           links.c.dunning_notice_id.isnot(None),
                                           links.c.meeting_id.isnot(None))))
    with op.batch_alter_table('document_links', schema=None) as batch_op:
        for column, _table, suffix in reversed(_LINK_TARGETS):
            batch_op.drop_index(batch_op.f(f'ix_document_links_{column}'))
            batch_op.drop_constraint(f'uq_document_links_{suffix}', type_='unique')
            batch_op.drop_constraint(f'fk_document_links_{column}', type_='foreignkey')
            batch_op.drop_column(column)
