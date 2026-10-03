"""[oss-v1.45.0] Belegablage: documents, document_links, document_events; incoming_invoices schlank

Bisher konnte man nur E-Rechnungen ablegen (``incoming_invoices``: Datei, Status und
Buchungs-FKs an derselben Zeile). Neu: jede Datei ist ein ``documents``-Eintrag
(Rechnung, Kassenbon, Kontoauszug, Scan …); die Verknuepfung zu Buchungen bzw.
Sammelbuchungen steht in ``document_links``, das Aktionsprotokoll in
``document_events``. ``incoming_invoices`` behaelt nur die aus dem Original gelesenen
Daten plus ``document_id``.

Datenmigration (verlustfrei): je ``incoming_invoices``-Zeile ein Beleg.

* Status ``Verbucht`` → ``Neu`` + ``document_links``-Zeile (verbucht ist jetzt abgeleitet:
  es gibt eine wirksame Verknuepfung), ``Verworfen`` bleibt, ``Neu`` bleibt.
* ``file_path`` (absolut) → ``storage_key`` (relativ, ab ``incoming/``); die Dateien
  bleiben, wo sie sind. Ein Pfad ohne ``/incoming/``-Segment wird NULL (= Datei fehlt).
* Protokoll: ``uploaded`` (+ ``linked`` bei verbuchten Belegen — was einmal verknuepft
  war, bleibt unloeschbar).

``incoming_invoices`` wird **neu aufgebaut** statt Spalten zu droppen: die Fremdschluessel
der Vorgaenger-Migration sind unbenannt, und MariaDB kann eine FK-Spalte ohne
Constraint-Namen nicht droppen. Alle neuen Constraints sind benannt.

``downgrade()`` ist best-effort: Belege ohne E-Rechnungsdaten gehen verloren (ihre
Dateien bleiben auf der Platte), ``file_path`` wird NULL (der absolute Pfad laesst sich
hier nicht rekonstruieren).

Dialekt-portabel: SQLAlchemy Core, alle NOT-NULL-Spalten explizit gesetzt.

Revision ID: c4f8a2d6b1e9
Revises: b9e5a7c3d2f8
Create Date: 2026-10-02 09:00:00.000000

"""
import json
import os
import re
from datetime import datetime

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4f8a2d6b1e9'
down_revision = 'b9e5a7c3d2f8'
branch_labels = None
depends_on = None


# Bewusst hier dupliziert statt aus app.documents importiert: eine Migration muss auch dann
# noch laufen, wenn sich der App-Code spaeter aendert.
_INCOMING_KEY_RE = re.compile(
    r"^incoming/(?:[0-9]{4}|ohne-datum)/[0-9]{1,12}_[0-9a-f]{8}_[A-Za-z0-9._-]{1,200}$")

_meta = sa.MetaData()

# Alte Tabelle (nur die gelesenen Spalten) — fuer Upgrade-Quelle und Downgrade-Ziel.
_old = sa.Table(
    'incoming_invoices', _meta,
    sa.Column('id', sa.Integer, primary_key=True),
    sa.Column('created_at', sa.DateTime),
    sa.Column('created_by_id', sa.Integer),
    sa.Column('original_name', sa.String(255)),
    sa.Column('file_path', sa.String(500)),
    sa.Column('sha256', sa.String(64)),
    sa.Column('source_kind', sa.String(10)),
    sa.Column('syntax', sa.String(10)),
    sa.Column('guideline', sa.String(255)),
    sa.Column('status', sa.String(20)),
    sa.Column('number', sa.String(100)),
    sa.Column('issue_date', sa.Date),
    sa.Column('currency', sa.String(3)),
    sa.Column('type_code', sa.String(3)),
    sa.Column('seller_name', sa.String(200)),
    sa.Column('seller_vat_id', sa.String(30)),
    sa.Column('grand_total', sa.Numeric(12, 2)),
    sa.Column('data', sa.Text),
    sa.Column('supplier_id', sa.Integer),
    sa.Column('booking_id', sa.Integer),
    sa.Column('booking_group_id', sa.Integer),
)

_documents = sa.Table(
    'documents', _meta,
    sa.Column('id', sa.Integer, primary_key=True),
    sa.Column('area', sa.String(20)),
    sa.Column('kind', sa.String(20)),
    sa.Column('status', sa.String(20)),
    sa.Column('storage_key', sa.String(255)),
    sa.Column('original_name', sa.String(255)),
    sa.Column('content_type', sa.String(100)),
    sa.Column('size_bytes', sa.BigInteger),
    sa.Column('sha256', sa.String(64)),
    sa.Column('title', sa.String(200)),
    sa.Column('number', sa.String(100)),
    sa.Column('document_date', sa.Date),
    sa.Column('amount', sa.Numeric(12, 2)),
    sa.Column('supplier_id', sa.Integer),
    sa.Column('created_at', sa.DateTime),
    sa.Column('created_by_id', sa.Integer),
)

_links = sa.Table(
    'document_links', _meta,
    sa.Column('id', sa.Integer, primary_key=True),
    sa.Column('document_id', sa.Integer),
    sa.Column('booking_id', sa.Integer),
    sa.Column('booking_group_id', sa.Integer),
    sa.Column('created_at', sa.DateTime),
    sa.Column('created_by_id', sa.Integer),
)

_events = sa.Table(
    'document_events', _meta,
    sa.Column('id', sa.Integer, primary_key=True),
    sa.Column('document_id', sa.Integer),
    sa.Column('action', sa.String(30)),
    sa.Column('document_name', sa.String(255)),
    sa.Column('document_sha256', sa.String(64)),
    sa.Column('detail', sa.Text),
    sa.Column('user_id', sa.Integer),
    sa.Column('created_at', sa.DateTime),
)

def _slim_table():
    """Die schlanke ``incoming_invoices`` (Upgrade-Ziel, Downgrade-Quelle) als Core-Tabelle."""
    return sa.Table(
        'incoming_invoices', sa.MetaData(),
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('document_id', sa.Integer),
        sa.Column('source_kind', sa.String(10)),
        sa.Column('syntax', sa.String(10)),
        sa.Column('guideline', sa.String(255)),
        sa.Column('number', sa.String(100)),
        sa.Column('issue_date', sa.Date),
        sa.Column('currency', sa.String(3)),
        sa.Column('type_code', sa.String(3)),
        sa.Column('seller_name', sa.String(200)),
        sa.Column('seller_vat_id', sa.String(30)),
        sa.Column('grand_total', sa.Numeric(12, 2)),
        sa.Column('data', sa.Text),
    )


def _storage_key(file_path):
    """Relativer Schluessel ab ``incoming/`` oder None, wenn der Pfad nicht passt."""
    if not file_path:
        return None
    normalized = file_path.replace("\\", "/")
    idx = normalized.rfind("/incoming/")
    if idx < 0:
        return None
    key = normalized[idx + 1:]
    return key if _INCOMING_KEY_RE.fullmatch(key) else None


def _size(file_path):
    try:
        return os.path.getsize(file_path) if file_path else 0
    except OSError:
        return 0


def _create_new_tables():
    op.create_table(
        'documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('area', sa.String(length=20), server_default='accounting', nullable=False),
        sa.Column('kind', sa.String(length=20), server_default='other', nullable=False),
        sa.Column('status', sa.String(length=20), server_default='Neu', nullable=False),
        sa.Column('storage_key', sa.String(length=255), nullable=True),
        sa.Column('original_name', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=True),
        sa.Column('number', sa.String(length=100), nullable=True),
        sa.Column('document_date', sa.Date(), nullable=True),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('supplier_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['supplier_id'], ['customers.id'], name='fk_documents_supplier_id'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_documents_created_by_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('sha256', name='uq_documents_sha256'),
        sa.UniqueConstraint('storage_key', name='uq_documents_storage_key'),
    )
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_documents_status'), ['status'], unique=False)

    op.create_table(
        'document_links',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('booking_id', sa.Integer(), nullable=True),
        sa.Column('booking_group_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'],
                                name='fk_document_links_document_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'],
                                name='fk_document_links_booking_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['booking_group_id'], ['booking_groups.id'],
                                name='fk_document_links_booking_group_id', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name='fk_document_links_created_by_id'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id', 'booking_id', name='uq_document_links_booking'),
        sa.UniqueConstraint('document_id', 'booking_group_id', name='uq_document_links_group'),
    )
    with op.batch_alter_table('document_links', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_document_links_document_id'), ['document_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_document_links_booking_id'), ['booking_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_document_links_booking_group_id'),
                              ['booking_group_id'], unique=False)

    op.create_table(
        'document_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=True),
        sa.Column('action', sa.String(length=30), nullable=False),
        sa.Column('document_name', sa.String(length=255), nullable=True),
        sa.Column('document_sha256', sa.String(length=64), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'],
                                name='fk_document_events_document_id', ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_document_events_user_id'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('document_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_document_events_document_id'), ['document_id'], unique=False)


def _create_slim_incoming():
    op.create_table(
        'incoming_invoices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('source_kind', sa.String(length=10), nullable=False),
        sa.Column('syntax', sa.String(length=10), nullable=False),
        sa.Column('guideline', sa.String(length=255), nullable=True),
        sa.Column('number', sa.String(length=100), nullable=True),
        sa.Column('issue_date', sa.Date(), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('type_code', sa.String(length=3), nullable=True),
        sa.Column('seller_name', sa.String(length=200), nullable=True),
        sa.Column('seller_vat_id', sa.String(length=30), nullable=True),
        sa.Column('grand_total', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('data', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'],
                                name='fk_incoming_invoices_document_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('document_id', name='uq_incoming_invoices_document_id'),
    )


def upgrade():
    bind = op.get_bind()
    _create_new_tables()

    # --- Daten sichern: alte Zeilen lesen, Belege/Links/Ereignisse anlegen -----------
    old_rows = [dict(r._mapping) for r in bind.execute(sa.select(_old).order_by(_old.c.id))]
    slim = []                      # (document_id, Zeile) fuer die neue incoming_invoices
    seen_sha = {}                  # sha256 -> document_id (die alte Tabelle hatte keinen Unique)
    now = datetime.utcnow()
    for row in old_rows:
        sha = row['sha256']
        if sha in seen_sha:
            continue               # Dublette (Race beim Upload) — die erste Zeile gilt
        created = row['created_at'] or now
        booked = row['status'] == 'Verbucht'
        status = 'Verworfen' if row['status'] == 'Verworfen' else 'Neu'
        is_credit = row['type_code'] in ('381', '261')
        key = _storage_key(row['file_path'])
        doc_id = bind.execute(_documents.insert().values(
            area='accounting', kind='credit_note' if is_credit else 'invoice', status=status,
            storage_key=key, original_name=row['original_name'],
            content_type='application/pdf' if row['source_kind'] == 'pdf' else 'application/xml',
            size_bytes=_size(row['file_path']) if key else 0, sha256=sha,
            title=row['seller_name'], number=row['number'], document_date=row['issue_date'],
            amount=abs(row['grand_total']) if row['grand_total'] is not None else None,
            supplier_id=row['supplier_id'], created_at=created, created_by_id=row['created_by_id'],
        )).inserted_primary_key[0]
        seen_sha[sha] = doc_id
        detail = json.dumps({"migriert": "Eingangsrechnung"}, ensure_ascii=False)
        bind.execute(_events.insert().values(
            document_id=doc_id, action='uploaded', document_name=row['original_name'],
            document_sha256=sha, detail=detail, user_id=row['created_by_id'], created_at=created))
        if booked and (row['booking_id'] or row['booking_group_id']):
            bind.execute(_links.insert().values(
                document_id=doc_id, booking_id=row['booking_id'],
                booking_group_id=None if row['booking_id'] else row['booking_group_id'],
                created_at=created, created_by_id=row['created_by_id']))
            bind.execute(_events.insert().values(
                document_id=doc_id, action='linked', document_name=row['original_name'],
                document_sha256=sha, detail=detail, user_id=row['created_by_id'], created_at=created))
        slim.append((doc_id, row))

    # --- incoming_invoices neu aufbauen ----------------------------------------------
    op.drop_table('incoming_invoices')      # nimmt den Index ix_incoming_invoices_sha256 mit
    _create_slim_incoming()
    slim_table = _slim_table()
    for doc_id, row in slim:
        bind.execute(slim_table.insert().values(
            document_id=doc_id, source_kind=row['source_kind'], syntax=row['syntax'],
            guideline=row['guideline'], number=row['number'], issue_date=row['issue_date'],
            currency=row['currency'], type_code=row['type_code'], seller_name=row['seller_name'],
            seller_vat_id=row['seller_vat_id'], grand_total=row['grand_total'], data=row['data']))


def downgrade():
    bind = op.get_bind()
    slim = _slim_table()
    rows = [dict(r._mapping) for r in bind.execute(sa.select(slim).order_by(slim.c.id))]
    docs = {r.id: dict(r._mapping) for r in bind.execute(sa.select(_documents))}
    linked = {}
    for link in bind.execute(sa.select(_links).order_by(_links.c.id)):
        linked.setdefault(link.document_id, link)

    op.drop_table('incoming_invoices')
    op.create_table(
        'incoming_invoices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('original_name', sa.String(length=255), nullable=False),
        sa.Column('file_path', sa.String(length=500), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('source_kind', sa.String(length=10), nullable=False),
        sa.Column('syntax', sa.String(length=10), nullable=False),
        sa.Column('guideline', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('number', sa.String(length=100), nullable=True),
        sa.Column('issue_date', sa.Date(), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('type_code', sa.String(length=3), nullable=True),
        sa.Column('seller_name', sa.String(length=200), nullable=True),
        sa.Column('seller_vat_id', sa.String(length=30), nullable=True),
        sa.Column('grand_total', sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column('data', sa.Text(), nullable=False),
        sa.Column('supplier_id', sa.Integer(), nullable=True),
        sa.Column('booking_id', sa.Integer(), nullable=True),
        sa.Column('booking_group_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['booking_group_id'], ['booking_groups.id']),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
        sa.ForeignKeyConstraint(['supplier_id'], ['customers.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('incoming_invoices', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incoming_invoices_sha256'), ['sha256'], unique=False)
    for row in rows:
        doc = docs.get(row['document_id'])
        if doc is None:
            continue
        link = linked.get(doc['id'])
        if link is not None:
            status = 'Verbucht'
        else:
            status = 'Verworfen' if doc['status'] == 'Verworfen' else 'Neu'
        bind.execute(_old.insert().values(
            created_at=doc['created_at'], created_by_id=doc['created_by_id'],
            original_name=doc['original_name'], file_path=None, sha256=doc['sha256'],
            source_kind=row['source_kind'], syntax=row['syntax'], guideline=row['guideline'],
            status=status, number=row['number'], issue_date=row['issue_date'],
            currency=row['currency'], type_code=row['type_code'], seller_name=row['seller_name'],
            seller_vat_id=row['seller_vat_id'], grand_total=row['grand_total'], data=row['data'],
            supplier_id=doc['supplier_id'],
            booking_id=link.booking_id if link is not None else None,
            booking_group_id=link.booking_group_id if link is not None else None))

    with op.batch_alter_table('document_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_events_document_id'))
    op.drop_table('document_events')
    with op.batch_alter_table('document_links', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_links_booking_group_id'))
        batch_op.drop_index(batch_op.f('ix_document_links_booking_id'))
        batch_op.drop_index(batch_op.f('ix_document_links_document_id'))
    op.drop_table('document_links')
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_documents_status'))
    op.drop_table('documents')
