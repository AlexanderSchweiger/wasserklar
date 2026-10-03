"""Archivfassung der Rechnungen und Mahnungen im Dokumentenregister (app/invoices/archive.py).

Gesperrte Rechnungen (und Entwuerfe im Moment des Versands) bekommen ihre Dateien als ``Document``
im Bereich ``invoices``; die Pfadspalte zeigt auf genau diese Datei. Entwurfsausdrucke werden nicht
abgelegt. Fehlt eine Archivdatei, entsteht ein neues Dokument mit ``neu_erzeugt`` im Protokoll.
"""
import io
import sys
import types
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.documents import storage
from app.extensions import db
from app.invoices import routes as inv_routes
from app.invoices.archive import archive_invoice_file
from app.models import (
    BillingPeriod, Customer, Document, DocumentLink, DunningNotice, Invoice, InvoiceItem, User,
)
from tests.conftest import _ensure_role


@pytest.fixture
def fake_weasyprint(monkeypatch):
    """WeasyPrint-Ersatz: je Aufruf ein anderes (gueltiges) PDF — wie im echten Leben, wo jede
    Rechnung andere Bytes ergibt."""
    counter = {"n": 0}

    def _pdf():
        counter["n"] += 1
        writer = PdfWriter()
        writer.add_blank_page(width=500 + counter["n"], height=842)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    class FakeDocument:
        def __init__(self):
            self.metadata = types.SimpleNamespace(attachments=[], xmp_metadata=[])

        def write_pdf(self, target=None, **kwargs):
            data = _pdf()
            if target is None:
                return data
            Path(target).write_bytes(data)
            return None

    class FakeHTML:
        def __init__(self, string=None, **kwargs):
            self._document = FakeDocument()

        def render(self, **kwargs):
            return self._document

        def write_pdf(self, target=None, **kwargs):
            return self._document.write_pdf(target, **kwargs)

    module = types.ModuleType("weasyprint")
    module.HTML = FakeHTML
    module.Attachment = lambda **kw: types.SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, "weasyprint", module)
    return counter


@pytest.fixture
def tenant(app, tmp_path, monkeypatch):
    folder = tmp_path / "tenant" / "pdfs"
    folder.mkdir(parents=True)
    monkeypatch.setitem(app.config, "PDF_DIR", str(folder))
    return tmp_path / "tenant"


@pytest.fixture
def admin(app):
    user = User(username="admin", email="admin@a.test", role_id=_ensure_role("Admin").id)
    user.set_password("secret")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client):
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})


def _invoice(number, status, customer, period, **extra):
    inv = Invoice(invoice_number=number, customer_id=customer.id, billing_period_id=period.id,
                  date=date(2026, 3, 1), due_date=date(2026, 3, 31), status=status, **extra)
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(invoice_id=inv.id, description="Wasser", quantity=Decimal("40"), unit="m³",
                               unit_price=Decimal("2.5"), amount=Decimal("100"), tax_rate=Decimal("10")))
    db.session.flush()
    inv.recalculate_total()
    return inv


@pytest.fixture
def invoices(app):
    period = BillingPeriod(name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), active=True)
    customer = Customer(name="Kunde Test", customer_number=7, email="kunde@test.local", rechnung_per_email=True)
    db.session.add_all([period, customer])
    db.session.flush()
    sent = _invoice("2026-00042", Invoice.STATUS_SENT, customer, period)
    draft = _invoice("2026-00043", Invoice.STATUS_DRAFT, customer, period)
    db.session.commit()
    return sent, draft


def _files(tenant):
    return sorted(p.relative_to(tenant).as_posix() for p in tenant.rglob("*") if p.is_file())


class TestSinglePdf:
    def test_a_locked_invoice_is_archived_once(self, client, tenant, admin, invoices, fake_weasyprint):
        sent, _draft = invoices
        _login(client)
        first = client.get(f"/invoices/{sent.id}/pdf")
        assert first.status_code == 200
        doc = Document.query.one()
        assert doc.area == Document.AREA_INVOICES and doc.kind == Document.KIND_SALES_INVOICE
        assert doc.number == "2026-00042" and doc.document_date == date(2026, 3, 1)
        assert doc.storage_key.startswith("documents/") and first.data == storage.read(doc.storage_key)
        assert [row.invoice_id for row in doc.links] == [sent.id]
        assert db.session.get(Invoice, sent.id).pdf_path == str(storage.path_for(doc.storage_key))
        again = client.get(f"/invoices/{sent.id}/pdf")
        assert again.data == first.data and Document.query.count() == 1      # Archiv, nicht neu gerendert

    def test_a_draft_print_is_not_stored(self, client, tenant, admin, invoices, fake_weasyprint):
        _sent, draft = invoices
        _login(client)
        r = client.get(f"/invoices/{draft.id}/pdf")
        assert r.status_code == 200 and r.data.startswith(b"%PDF")
        assert Document.query.count() == 0 and db.session.get(Invoice, draft.id).pdf_path is None
        assert _files(tenant) == []

    def test_a_missing_archive_file_is_regenerated_and_marked(self, client, tenant, admin, invoices,
                                                              fake_weasyprint):
        sent, _draft = invoices
        _login(client)
        client.get(f"/invoices/{sent.id}/pdf")
        original = Document.query.one()
        storage.delete(original.storage_key)
        client.get(f"/invoices/{sent.id}/pdf")
        docs = Document.query.order_by(Document.id).all()
        assert len(docs) == 2
        archived = [e for e in docs[1].events if e.action == "archived"][0]
        assert archived.detail_dict.get("neu_erzeugt") is True
        page = client.get(f"/invoices/{sent.id}").get_data(as_text=True)
        assert "neu erzeugt" in page and "Archiv" in page


class TestBulkAndSend:
    def test_zip_archives_locked_and_skips_drafts(self, client, tenant, admin, invoices, fake_weasyprint):
        sent, draft = invoices
        _login(client)
        r = client.post("/invoices/bulk-pdf-zip", data={"invoice_ids": [sent.id, draft.id]})
        assert r.status_code == 200
        assert [link.invoice_id for link in DocumentLink.query.all()] == [sent.id]
        assert db.session.get(Invoice, draft.id).pdf_path is None

    def test_email_archives_the_sent_version(self, client, tenant, admin, invoices, fake_weasyprint, monkeypatch):
        _sent, draft = invoices
        mails = []
        monkeypatch.setattr(inv_routes, "send_mail", mails.append)
        _login(client)
        client.post(f"/invoices/{draft.id}/send-email")
        assert len(mails) == 1
        attached = next(a for a in mails[0].attachments if a.filename.endswith(".pdf"))
        doc = Document.query.one()
        assert storage.read(doc.storage_key) == attached.data
        invoice = db.session.get(Invoice, draft.id)
        assert invoice.status == Invoice.STATUS_SENT and invoice.pdf_path == str(storage.path_for(doc.storage_key))

    def test_a_test_mail_archives_nothing(self, client, tenant, admin, invoices, fake_weasyprint, monkeypatch):
        _sent, draft = invoices
        monkeypatch.setattr(inv_routes, "send_mail", lambda msg: None)
        _login(client)
        client.post(f"/invoices/{draft.id}/send-email", data={"test_mode": "1"})
        assert Document.query.count() == 0 and _files(tenant) == []


class TestArchiveCardAndDownload:
    def test_card_and_download(self, client, tenant, admin, invoices, fake_weasyprint):
        sent, _draft = invoices
        _login(client)
        pdf = client.get(f"/invoices/{sent.id}/pdf").data
        doc = Document.query.one()
        page = client.get(f"/invoices/{sent.id}").get_data(as_text=True)
        assert "Archiv" in page and doc.sha256[:16] in page and "aktuell" in page
        r = client.get(f"/invoices/{sent.id}/archive/{doc.id}")
        assert r.status_code == 200 and r.data == pdf
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_download_only_through_its_own_invoice(self, client, tenant, admin, invoices, fake_weasyprint):
        sent, draft = invoices
        _login(client)
        client.get(f"/invoices/{sent.id}/pdf")
        doc = Document.query.one()
        assert client.get(f"/invoices/{draft.id}/archive/{doc.id}").status_code == 404

    def test_no_card_without_archive(self, client, tenant, admin, invoices):
        _sent, draft = invoices
        _login(client)
        page = client.get(f"/invoices/{draft.id}").get_data(as_text=True)
        assert "Aufbewahren bis" not in page


class TestArchiveHelper:
    def test_xml_and_credit_notes(self, app, tenant, invoices):
        sent, draft = invoices
        draft.invoice_kind = Invoice.KIND_CREDIT_NOTE
        archive_invoice_file(sent, b"<Invoice/>", "xml")
        archive_invoice_file(draft, b"<CreditNote/>", "xml")
        db.session.commit()
        by_number = {d.number: d for d in Document.query.all()}
        assert by_number["2026-00042"].kind == Document.KIND_SALES_INVOICE
        assert by_number["2026-00043"].kind == Document.KIND_SALES_CREDIT
        assert by_number["2026-00042"].content_type == "application/xml"
        assert sent.xml_path == str(storage.path_for(by_number["2026-00042"].storage_key))

    def test_strict_false_swallows_write_errors(self, app, tenant, invoices, monkeypatch):
        sent, _draft = invoices
        monkeypatch.setattr(storage, "put", lambda key, data: (_ for _ in ()).throw(storage.StorageError("voll")))
        assert archive_invoice_file(sent, b"%PDF-x", "pdf", strict=False) is None
        with pytest.raises(OSError):
            archive_invoice_file(sent, b"%PDF-y", "pdf")


class TestDunning:
    def test_frozen_notice_lands_in_the_register(self, app, tenant, admin, invoices):
        from app.dunning.routes import _freeze_dunning_document
        sent, _draft = invoices
        notice = DunningNotice(invoice_id=sent.id, level_snapshot=1, name_snapshot="1. Mahnung",
                               issued_date=date(2026, 4, 10))
        db.session.add(notice)
        db.session.flush()
        with app.test_request_context():
            path = _freeze_dunning_document(notice, "pdf", b"%PDF-1.7 mahnung")
        db.session.commit()
        doc = Document.query.one()
        assert doc.area == Document.AREA_DUNNING and doc.kind == Document.KIND_DUNNING_NOTICE
        assert doc.original_name == "2026-00042_M1.pdf" and doc.document_date == date(2026, 4, 10)
        assert notice.pdf_path == path == str(storage.path_for(doc.storage_key))
        assert [row.dunning_notice_id for row in doc.links] == [notice.id]
