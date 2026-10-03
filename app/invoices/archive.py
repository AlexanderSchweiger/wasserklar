"""Archivfassung einer Rechnung im Dokumentenregister — der **einzige** Weg zu ``pdf_path``/``doc_path``/``xml_path``.

Eine gesperrte Rechnung (alles ausser Entwurf) bzw. ein Entwurf im Moment des Versands bekommt
ihre Dateien als ``Document`` (Bereich ``invoices``, Art Ausgangsrechnung bzw. Gutschrift):
unveraenderlich, mit Pruefsumme, Protokoll und Aufbewahrungsfrist (AT 7 / DE 8 Jahre). Die
Pfadspalten bleiben fuer die bestehenden Lesestellen bestehen und zeigen auf genau diese Datei.

Entwurfsausdrucke werden **nicht** abgelegt (``invoice_needs_archive``) — sie gehen direkt aus dem
Speicher an den Browser. Fehlt die Archivdatei einer gesperrten Rechnung, wird sie neu erzeugt;
das neue Dokument traegt dann im Protokoll ``neu_erzeugt`` — es ist nicht das Original (die
Stammdaten koennen sich seitdem geaendert haben).
"""
from flask import current_app, has_request_context

from app.documents import service as doc_svc
from app.documents import storage
from app.file_safety import safe_tenant_path
from app.models import Document, Invoice

PATH_COLUMNS = {"pdf": "pdf_path", "docx": "doc_path", "xml": "xml_path"}


class ArchiveError(OSError):
    """Die Archivfassung konnte nicht geschrieben werden. Erbt von ``OSError``: die Aufrufer fangen
    Schreibfehler seit jeher als ``OSError`` (``open(..., "wb")`` vor dem Register)."""


def invoice_needs_archive(invoice):
    """Wird diese Fassung archiviert? Ja fuer jede gesperrte Rechnung (alles ausser Entwurf)."""
    return invoice.status != Invoice.STATUS_DRAFT


def _current_user_id():
    if not has_request_context():
        return None
    from flask_login import current_user
    return current_user.id if getattr(current_user, "is_authenticated", False) else None


def archive_invoice_file(invoice, data, ext, *, strict=True):
    """Legt eine Fassung (``pdf`` | ``docx`` | ``xml``) der Rechnung im Register ab, verknuepft sie mit
    der Rechnung und setzt die Pfadspalte. Gibt den Pfad zurueck. Committet nicht.

    ``strict=False`` (nach einem schon erfolgten Versand): ein Schreibfehler wird protokolliert und
    ``None`` zurueckgegeben, statt den Vorgang abzubrechen — die Mail ist dann schon draussen.
    """
    column = PATH_COLUMNS[ext]
    previous = getattr(invoice, column)
    detail = {}
    if previous and not safe_tenant_path(previous):
        detail = {"neu_erzeugt": True, "grund": "Die bisherige Archivdatei fehlt — nicht das Original."}
    kind = (Document.KIND_SALES_CREDIT if invoice.invoice_kind == Invoice.KIND_CREDIT_NOTE
            else Document.KIND_SALES_INVOICE)
    try:
        doc = doc_svc.store_generated(
            Document.AREA_INVOICES, kind, data, ext,
            original_name=f"{invoice.invoice_number}.{ext}",
            title=invoice.customer.letter_name if invoice.customer else None,
            number=invoice.invoice_number, document_date=invoice.date, amount=invoice.total_amount,
            user_id=_current_user_id(), event_detail=detail, invoice=invoice)
    except doc_svc.DocumentError as exc:
        if strict:
            raise ArchiveError(str(exc)) from exc
        current_app.logger.error("Archivfassung %s.%s nicht abgelegt: %s", invoice.invoice_number, ext, exc)
        return None
    path = str(storage.path_for(doc.storage_key))
    setattr(invoice, column, path)
    return path


def archived_documents(invoice):
    """Alle Archivdateien der Rechnung (neueste zuerst) fuer die Karte „Archiv“ der Rechnungsseite."""
    docs = {row.document for row in invoice.document_links}
    return sorted(docs, key=lambda d: (d.created_at, d.id), reverse=True)


def is_current(invoice, doc):
    """Ist ``doc`` die Fassung, die die Rechnung derzeit ausliefert (Pfadspalte zeigt auf sie)?"""
    path = storage.path_for(doc.storage_key) if doc.storage_key else None
    if path is None:
        return False
    current = {safe_tenant_path(getattr(invoice, c)) for c in PATH_COLUMNS.values()}
    return str(path) in current
