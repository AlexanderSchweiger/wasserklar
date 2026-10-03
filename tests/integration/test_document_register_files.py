"""``documents-register-files``: Altbestand (Pfadspalten) ins Dokumentenregister uebernehmen.

Die Dateien bleiben liegen (Altschluessel), jede bekommt Pruefsumme, Verknuepfung und das Ereignis
``registered``; fehlende Dateien werden gemeldet; ein zweiter Lauf aendert nichts mehr.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.documents import storage
from app.documents.register import register_existing_files
from app.extensions import db
from app.models import (
    Customer, Document, DunningNotice, FeaturePhoto, Invoice, Meeting, MeetingProtocol, SchriftverkehrDocument,
)


@pytest.fixture
def tenant(app, tmp_path, monkeypatch):
    folder = tmp_path / "tenant" / "pdfs"
    folder.mkdir(parents=True)
    monkeypatch.setitem(app.config, "PDF_DIR", str(folder))
    return tmp_path / "tenant"


def _file(tenant, rel, data):
    path = tenant / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


@pytest.fixture
def legacy(app, tenant):
    customer = Customer(name="Alt Kunde")
    db.session.add(customer)
    db.session.flush()
    invoice = Invoice(invoice_number="2023-00001", customer_id=customer.id, date=date(2023, 4, 1),
                      status="Versendet", total_amount=Decimal("50"),
                      pdf_path=_file(tenant, "pdfs/2023/2023-00001.pdf", b"%PDF-alt-rechnung"),
                      xml_path=_file(tenant, "pdfs/2023/2023-00001.xml", b"<Invoice/>"))
    gone = Invoice(invoice_number="2023-00002", customer_id=customer.id, date=date(2023, 4, 2),
                   status="Versendet", pdf_path=str(tenant / "pdfs" / "2023" / "2023-00002.pdf"))
    db.session.add_all([invoice, gone])
    db.session.flush()
    notice = DunningNotice(invoice_id=invoice.id, level_snapshot=1, name_snapshot="1. Mahnung",
                           issued_date=date(2023, 6, 1),
                           pdf_path=_file(tenant, "pdfs/2023/dunning/2023-00001_M1.pdf", b"%PDF-mahnung"))
    meeting = Meeting(meeting_type="board", title="Vorstand", status="held", meeting_date=date(2023, 2, 1))
    db.session.add_all([notice, meeting])
    db.session.flush()
    protocol = MeetingProtocol(meeting_id=meeting.id, status="final",
                               file_path=_file(tenant, "schriftverkehr/2023/Protokoll_Vorstand_2023-02-01.pdf",
                                               b"%PDF-protokoll"))
    letter = SchriftverkehrDocument(year=2023, title="Bescheid", doc_type="incoming",
                                    file_path=_file(tenant, "schriftverkehr/2023/Bescheid.pdf", b"%PDF-brief"))
    photo = FeaturePhoto(feature_id=1, filename="abc.jpg")
    _file(tenant, "network/abc.jpg", b"\xff\xd8\xff" + b"0" * 97)
    db.session.add_all([protocol, letter, photo])
    db.session.commit()
    return invoice, notice, meeting, letter, photo


def test_registers_everything_in_place(app, tenant, legacy):
    invoice, notice, meeting, letter, photo = legacy
    report = register_existing_files()
    assert report.registered == 5 and report.photos_measured == 1
    assert [what for what, _id, _text in report.missing] == ["Rechnung"]          # 2023-00002 fehlt

    by_key = {d.storage_key: d for d in Document.query.all()}
    pdf = by_key["pdfs/2023/2023-00001.pdf"]
    assert pdf.area == Document.AREA_INVOICES and pdf.kind == Document.KIND_SALES_INVOICE
    assert pdf.document_date == date(2023, 4, 1) and [row.invoice_id for row in pdf.links] == [invoice.id]
    assert storage.read(pdf.storage_key) == b"%PDF-alt-rechnung"                   # liegt, wo es lag
    assert [e.action for e in pdf.events][:1] == ["registered"]
    assert by_key["pdfs/2023/2023-00001.xml"].content_type == "application/xml"
    assert by_key["pdfs/2023/dunning/2023-00001_M1.pdf"].links[0].dunning_notice_id == notice.id
    protocol_doc = by_key["schriftverkehr/2023/Protokoll_Vorstand_2023-02-01.pdf"]
    assert protocol_doc.kind == Document.KIND_PROTOCOL and protocol_doc.links[0].meeting_id == meeting.id
    assert db.session.get(SchriftverkehrDocument, letter.id).document.storage_key == "schriftverkehr/2023/Bescheid.pdf"
    assert db.session.get(FeaturePhoto, photo.id).size_bytes == 100


def test_second_run_changes_nothing(app, tenant, legacy):
    register_existing_files()
    count = Document.query.count()
    report = register_existing_files()
    assert report.registered == 0 and report.linked == 0 and Document.query.count() == count


def test_dry_run_writes_nothing(app, tenant, legacy):
    report = register_existing_files(dry_run=True)
    assert report.registered == 5
    assert Document.query.count() == 0
