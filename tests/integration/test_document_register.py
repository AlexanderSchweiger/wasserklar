"""Dokumentenregister: Bereiche/Rechte (access), Fristklassen (retention), Speicherbelegung (usage),
erzeugte Dokumente (store_generated) und die Abgrenzung der Belegablage auf den Bereich ``accounting``."""
import io
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest
from pypdf import PdfWriter

from app.auth.permissions import PERM_BUCHHALTUNG, PERM_RECHNUNGEN, PERM_SCHRIFTFUEHRUNG
from app.documents import access, retention, storage, usage
from app.documents import service as svc
from app.extensions import db
from app.models import (
    Account, AppSetting, Booking, Customer, Document, DocumentEvent, DocumentLink, DunningNotice,
    FeaturePhoto, IncidentPhoto, Invoice, Meeting, User,
)
from tests.conftest import _ensure_role


def pdf(n=0):
    writer = PdfWriter()
    writer.add_blank_page(width=100 + n, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def tenant(app, tmp_path, monkeypatch):
    folder = tmp_path / "tenant" / "pdfs"
    folder.mkdir(parents=True)
    monkeypatch.setitem(app.config, "PDF_DIR", str(folder))
    return tmp_path / "tenant"


def make_user(name, *perms):
    role = _ensure_role("Admin") if perms == ("admin",) else _ensure_role(f"Rolle {name}", perms)
    user = User(username=name, email=f"{name}@test.test", role_id=role.id)
    user.set_password("secret")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def invoice(app):
    customer = Customer(name="Muster Max")
    db.session.add(customer)
    db.session.flush()
    inv = Invoice(invoice_number="2024-00007", customer_id=customer.id, date=date(2024, 5, 2),
                  status=Invoice.STATUS_SENT, total_amount=Decimal("120.00"))
    db.session.add(inv)
    db.session.commit()
    return inv


def generated(invoice, n=0, **kw):
    doc = svc.store_generated(Document.AREA_INVOICES, Document.KIND_SALES_INVOICE, pdf(n), "pdf",
                              original_name=f"{invoice.invoice_number}.pdf", number=invoice.invoice_number,
                              document_date=invoice.date, invoice=invoice, **kw)
    db.session.commit()
    return doc


class TestAccess:
    def test_each_permission_sees_its_area(self, app):
        kassier = make_user("kassier", PERM_BUCHHALTUNG)
        rechnung = make_user("rechnung", PERM_RECHNUNGEN)
        schrift = make_user("schrift", PERM_SCHRIFTFUEHRUNG)
        assert access.visible_areas(kassier) == [Document.AREA_ACCOUNTING]
        assert access.visible_areas(rechnung) == [Document.AREA_INVOICES]
        assert access.visible_areas(schrift) == [Document.AREA_RECORDS]

    def test_admin_sees_everything(self, app):
        admin = make_user("chef", "admin")
        assert access.visible_areas(admin) == list(access.AREAS)

    def test_records_only_for_cooperatives(self, app):
        admin = make_user("chef", "admin")
        AppSetting.set("org.type", "utility")
        assert Document.AREA_RECORDS not in access.visible_areas(admin)

    def test_anonymous_sees_nothing(self, app):
        assert access.visible_areas(None) == []

    def test_can_view_and_filter(self, app, tenant, invoice):
        doc = generated(invoice)
        kassier = make_user("kassier", PERM_BUCHHALTUNG)
        rechnung = make_user("rechnung", PERM_RECHNUNGEN)
        assert access.can_view(rechnung, doc) and not access.can_view(kassier, doc)
        assert Document.query.filter(access.area_filter(kassier)).count() == 0
        assert Document.query.filter(access.area_filter(rechnung)).count() == 1


_SHA = itertools.count(1)


class TestRetention:
    def _doc(self, area, kind, **kw):
        doc = Document(area=area, kind=kind, original_name="x.pdf", content_type="application/pdf",
                       sha256=f"{next(_SHA):064d}", created_at=datetime(2026, 1, 5), **kw)
        db.session.add(doc)
        db.session.flush()
        return doc

    @pytest.mark.parametrize("country,tax,letter", [("AT", 7, 7), ("DE", 8, 6)])
    def test_classes_per_country(self, app, country, tax, letter):
        AppSetting.set("org.country", country)
        invoice_doc = self._doc(Document.AREA_INVOICES, Document.KIND_SALES_INVOICE, document_date=date(2020, 3, 1))
        dunning_doc = self._doc(Document.AREA_DUNNING, Document.KIND_DUNNING_NOTICE, document_date=date(2020, 3, 1))
        letter_doc = self._doc(Document.AREA_RECORDS, Document.KIND_CORRESPONDENCE, document_date=date(2020, 3, 1))
        assert retention.retention_end(invoice_doc) == date(2020 + tax, 12, 31)
        assert retention.retention_end(dunning_doc) == date(2020 + letter, 12, 31)
        assert retention.retention_end(letter_doc) == date(2020 + letter, 12, 31)

    def test_protocols_are_permanent(self, app):
        doc = self._doc(Document.AREA_RECORDS, Document.KIND_PROTOCOL, document_date=date(1990, 3, 1))
        assert retention.retention_end(doc) is None
        assert not retention.is_expired(doc, today=date(2100, 1, 1))
        assert "dauerhaft" in retention.hint(doc)

    def test_generated_documents_count_from_their_own_date(self, app):
        """Eine spaet nachgetragene Rechnungsdatei verlaengert die Frist nicht (anders als bei Belegen)."""
        doc = self._doc(Document.AREA_INVOICES, Document.KIND_SALES_INVOICE, document_date=date(2015, 3, 1))
        assert retention.retention_end(doc) == date(2022, 12, 31)          # AT: 2015 + 7

    def test_correspondence_without_date_counts_from_filing(self, app):
        doc = self._doc(Document.AREA_RECORDS, Document.KIND_CORRESPONDENCE)
        assert retention.retention_end(doc) == date(2033, 12, 31)          # Ablage 2026 + 7

    def test_expired_list_spans_areas_but_never_protocols(self, app, tenant, invoice):
        old = generated(invoice)
        old.document_date = date(2010, 1, 1)
        invoice.date = date(2010, 1, 1)
        protocol = self._doc(Document.AREA_RECORDS, Document.KIND_PROTOCOL, document_date=date(1990, 1, 1))
        protocol.created_at = datetime(1990, 1, 1)
        db.session.commit()
        everything = [doc for doc, _end in svc.expired_documents(areas=None)]
        assert old in everything and protocol not in everything
        assert svc.expired_documents() == []                    # Standard: nur Belege
        with pytest.raises(svc.DocumentError, match="dauerhaft"):
            svc.delete_expired(protocol)


class TestUsage:
    def test_counts_register_and_photos(self, app, tenant, invoice):
        generated(invoice)
        db.session.add(FeaturePhoto(feature_id=1, filename="a.jpg", size_bytes=1000))
        db.session.add(IncidentPhoto(incident_id=1, filename="b.jpg", size_bytes=500))
        db.session.add(IncidentPhoto(incident_id=1, filename="c.jpg", size_bytes=None))   # Bestand
        db.session.commit()
        doc_bytes = Document.query.one().size_bytes
        by_area = usage.usage_by_area()
        assert by_area[Document.AREA_INVOICES] == {"label": "Ausgangsrechnungen", "count": 1, "bytes": doc_bytes}
        assert by_area["network_photos"]["bytes"] == 1000 and by_area["incident_photos"]["count"] == 2
        assert usage.total_used() == doc_bytes + 1500
        assert svc.quota_status()["used"] == doc_bytes + 1500

    def test_generated_documents_ignore_the_quota_but_uploads_do_not(self, app, tenant, invoice, monkeypatch):
        monkeypatch.setattr(svc, "_quota_limit_bytes", lambda: 1)
        generated(invoice)                                        # kein QuotaExceeded
        with pytest.raises(svc.QuotaExceeded, match="Dokumentenspeicher ist voll"):
            svc.store_upload("beleg.pdf", pdf(9), None)


class TestStoreGenerated:
    def test_file_link_and_event(self, app, tenant, invoice):
        doc = generated(invoice)
        assert doc.area == Document.AREA_INVOICES and doc.status == Document.STATUS_FILED
        assert doc.storage_key.startswith("documents/") and storage.exists(doc.storage_key)
        assert [row.invoice for row in doc.links] == [invoice]
        assert invoice.document_links[0].document is doc
        assert [e.action for e in doc.events] == ["archived", "linked"]

    def test_same_bytes_reuse_the_document(self, app, tenant, invoice):
        first = generated(invoice)
        again = generated(invoice)
        assert again is first and Document.query.count() == 1 and len(first.links) == 1

    def test_rollback_removes_the_written_file(self, app, tenant, invoice):
        doc = svc.store_generated(Document.AREA_INVOICES, Document.KIND_SALES_INVOICE, pdf(3), "pdf",
                                  original_name="x.pdf", invoice=invoice)
        key = doc.storage_key
        assert storage.exists(key)
        db.session.rollback()
        assert not storage.exists(key) and Document.query.count() == 0

    def test_commit_keeps_the_file(self, app, tenant, invoice):
        doc = generated(invoice, n=4)
        db.session.rollback()                                     # nichts mehr offen
        assert storage.exists(doc.storage_key)

    def test_exactly_one_target(self, app, tenant, invoice):
        doc = generated(invoice)
        with pytest.raises(ValueError):
            svc.attach(doc, invoice=invoice, meeting=Meeting(meeting_type="board", title="x"))
        with pytest.raises(ValueError):
            svc.attach(doc)


class TestAccountingStaysAccounting:
    def test_belegablage_never_sees_other_areas(self, app, tenant, invoice):
        foreign = generated(invoice)
        beleg = svc.store_upload("beleg.pdf", pdf(7), None)
        assert svc.tab_counts()["filed"] == 0 and svc.tab_counts()["inbox"] == 1
        assert svc.inbox_choices() == [beleg]
        assert svc.by_ids([foreign.id, beleg.id]) == [beleg]
        assert svc.parse_document_ids([str(foreign.id)])[1] == "Ein gewählter Beleg existiert nicht mehr."

    def test_link_refuses_non_receipts(self, app, tenant, invoice):
        foreign = generated(invoice)
        account = Account(name="Erlöse", code="ERL")
        db.session.add(account)
        db.session.flush()
        booking = Booking(date=date(2024, 5, 3), account_id=account.id, amount=Decimal("120"), description="x")
        db.session.add(booking)
        db.session.commit()
        with pytest.raises(svc.DocumentError, match="Nur Belege"):
            svc.link(foreign, booking=booking)

    def test_uploading_an_own_invoice_as_receipt_is_refused_without_a_link(self, app, tenant, invoice):
        data = pdf(11)
        svc.store_generated(Document.AREA_INVOICES, Document.KIND_SALES_INVOICE, data, "pdf",
                            original_name="r.pdf", invoice=invoice)
        db.session.commit()
        with pytest.raises(svc.DocumentError, match="Ausgangsrechnungen") as exc:
            svc.store_upload("r.pdf", data, None)
        assert not isinstance(exc.value, svc.DuplicateUpload)

    def test_autofill_only_for_receipts(self, app, tenant, invoice):
        doc = generated(invoice)
        doc.text_content = "Rechnung Nr. 4711 vom 01.02.2024 Gesamt 99,00 EUR"
        assert svc.autofill(doc) == {}


class TestAccountingRoutes:
    def test_receipt_routes_404_for_other_areas(self, app, client, tenant, invoice):
        doc = generated(invoice)
        make_user("kassier", PERM_BUCHHALTUNG)
        client.get("/auth/logout")
        client.post("/auth/login", data={"username": "kassier", "password": "secret"})
        assert client.get(f"/accounting/documents/{doc.id}").status_code == 404
        assert client.get(f"/accounting/documents/{doc.id}/file").status_code == 404
        listing = client.get("/accounting/documents?tab=all").get_data(as_text=True)
        assert "2024-00007.pdf" not in listing
