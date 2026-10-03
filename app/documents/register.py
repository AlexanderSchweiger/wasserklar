"""Bestand ins Dokumentenregister uebernehmen (``flask documents-register-files``).

Vor dem Register lagen Rechnungs-, Mahnungs-, Protokoll- und Schriftverkehrsdateien nur als
Pfadspalten in der DB. Dieser Lauf traegt jede vorhandene Datei als ``Document`` nach — **die
Datei bleibt, wo sie ist** (Altschluessel ``pdfs/…`` bzw. ``schriftverkehr/…``, siehe
``storage._KEY_RE``), bekommt Pruefsumme, Groesse, Verknuepfung und das Ereignis ``registered``.
Dazu misst er die Groesse der Fotos aus Leitungsnetz und Stoerungsjournal nach (Kontingent).

Idempotent: was schon im Register steht (gleicher Schluessel oder gleiche Pruefsumme), wird nur
noch verknuepft bzw. uebersprungen. Fehlende Dateien werden gemeldet, nie erfunden.
"""
import hashlib
import os
from dataclasses import dataclass, field

from app.documents import service as svc
from app.documents import storage
from app.extensions import db
from app.file_safety import safe_tenant_path
from app.models import (
    Document, DunningNotice, FeaturePhoto, IncidentPhoto, Invoice, MeetingProtocol, SchriftverkehrDocument,
)

_BATCH = 50


@dataclass
class RegisterReport:
    registered: int = 0
    linked: int = 0
    already: int = 0
    photos_measured: int = 0
    missing: list = field(default_factory=list)       # [(Art, ID, Text)]

    def as_text(self):
        lines = [f"{self.registered} Datei(en) neu im Register, {self.linked} zusätzlich verknüpft, "
                 f"{self.already} schon vorhanden; {self.photos_measured} Foto(s) nachgemessen."]
        lines += [f"  [fehlt] {what} {obj_id}: {text}" for what, obj_id, text in self.missing]
        return "\n".join(lines)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _register(report, label, obj_id, path_value, *, area, kind, original_name, title=None, number=None,
              document_date=None, amount=None, **target):
    """Ein Pfadwert → Document (oder vorhandenes Document verknuepfen). Gibt das Document zurueck."""
    path = safe_tenant_path(path_value)
    if path is None:
        report.missing.append((label, obj_id, f"Datei fehlt oder liegt außerhalb der Ablage ({path_value})"))
        return None
    key = storage.key_for_path(path)
    if key is None:
        report.missing.append((label, obj_id, f"Dateiname passt nicht ins Register ({os.path.basename(path)})"))
        return None
    digest = _sha256(path)
    doc = Document.query.filter((Document.storage_key == key) | (Document.sha256 == digest)).first()
    if doc is not None:
        linked = any(getattr(row, name) is obj for row in doc.links for name, obj in target.items())
        if target and not linked:
            svc.attach(doc, **target)
            report.linked += 1
        else:
            report.already += 1
        return doc
    ext = key.rsplit(".", 1)[-1].lower() if "." in key.rsplit("/", 1)[-1] else ""
    doc = Document(
        area=area, kind=kind, status=Document.STATUS_FILED, storage_key=key,
        original_name=(original_name or os.path.basename(path))[:255],
        content_type=svc.CONTENT_TYPES.get(ext, "application/octet-stream"),
        size_bytes=os.path.getsize(path), sha256=digest, title=(title or "")[:200] or None,
        number=(number or "")[:100] or None, document_date=document_date, amount=amount)
    db.session.add(doc)
    db.session.flush()
    svc.log(doc, "registered", None, quelle="Bestand", schluessel=key)
    if target:
        svc.attach(doc, **target)
    report.registered += 1
    return doc


def _invoices(report):
    for invoice in Invoice.query.filter((Invoice.pdf_path.isnot(None)) | (Invoice.doc_path.isnot(None))
                                        | (Invoice.xml_path.isnot(None))).order_by(Invoice.id).all():
        kind = (Document.KIND_SALES_CREDIT if invoice.invoice_kind == Invoice.KIND_CREDIT_NOTE
                else Document.KIND_SALES_INVOICE)
        for column, ext in (("pdf_path", "pdf"), ("doc_path", "docx"), ("xml_path", "xml")):
            value = getattr(invoice, column)
            if value:
                _register(report, "Rechnung", invoice.invoice_number, value, area=Document.AREA_INVOICES,
                          kind=kind, original_name=f"{invoice.invoice_number}.{ext}",
                          title=invoice.customer.letter_name if invoice.customer else None,
                          number=invoice.invoice_number, document_date=invoice.date,
                          amount=invoice.total_amount, invoice=invoice)
        yield


def _dunning(report):
    for notice in DunningNotice.query.filter((DunningNotice.pdf_path.isnot(None))
                                             | (DunningNotice.doc_path.isnot(None))).order_by(DunningNotice.id).all():
        number = f"{notice.invoice.invoice_number} M{notice.level_snapshot}" if notice.invoice else None
        for column in ("pdf_path", "doc_path"):
            value = getattr(notice, column)
            if value:
                _register(report, "Mahnung", notice.id, value, area=Document.AREA_DUNNING,
                          kind=Document.KIND_DUNNING_NOTICE, original_name=os.path.basename(value),
                          number=number, document_date=notice.issued_date, dunning_notice=notice)
        yield


def _records(report):
    protocols = (MeetingProtocol.query.filter(MeetingProtocol.file_path.isnot(None))
                 .order_by(MeetingProtocol.id).all())
    for protocol in protocols:
        meeting = protocol.meeting
        _register(report, "Protokoll", protocol.meeting_id, protocol.file_path, area=Document.AREA_RECORDS,
                  kind=Document.KIND_PROTOCOL, original_name=protocol.original_filename,
                  title=meeting.title if meeting else None,
                  document_date=meeting.meeting_date if meeting else None, meeting=meeting)
        yield
    for entry in SchriftverkehrDocument.query.filter(SchriftverkehrDocument.document_id.is_(None)).order_by(
            SchriftverkehrDocument.id).all():
        doc = _register(report, "Schriftverkehr", entry.id, entry.file_path, area=Document.AREA_RECORDS,
                        kind=Document.KIND_CORRESPONDENCE, original_name=entry.original_filename,
                        title=entry.title, document_date=entry.document_date)
        if doc is not None and doc.correspondence is None:
            entry.document_id = doc.id
        yield


def _photos(report):
    from app.incidents.services import incident_upload_dir
    from app.network.services import technik_upload_dir
    for model, folder in ((FeaturePhoto, technik_upload_dir()), (IncidentPhoto, incident_upload_dir())):
        for photo in model.query.filter(model.size_bytes.is_(None)).order_by(model.id).all():
            path = os.path.join(folder, os.path.basename(photo.filename or ""))
            try:
                photo.size_bytes = os.path.getsize(path)
                report.photos_measured += 1
            except OSError:
                report.missing.append(("Foto", photo.id, f"Datei fehlt ({photo.filename})"))
            yield


def register_existing_files(*, dry_run=False):
    """Traegt den Bestand ins Register ein (siehe Modul-Doc). Committet in Etappen; ``dry_run``
    rollt am Ende alles zurueck und liefert nur den Bericht."""
    report = RegisterReport()
    steps = 0
    for source in (_invoices, _dunning, _records, _photos):
        for _ in source(report):
            steps += 1
            if not dry_run and steps % _BATCH == 0:
                db.session.commit()
    if dry_run:
        db.session.rollback()
    else:
        db.session.commit()
    return report
