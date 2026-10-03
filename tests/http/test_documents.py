"""Belegablage in der Oberflaeche: Eingang, Upload, Belegseite, Dateien, Buchen, Zuordnen."""
import base64
import io
from datetime import date
from decimal import Decimal

import pytest

from app.documents import service as svc
from app.documents import storage
from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, Document, DocumentEvent, DocumentLink, FiscalYear, User,
)
from tests.conftest import _ensure_role
from tests.integration.test_document_service import JPG, PNG, pdf
from tests.unit.test_einvoice_incoming import _fixture, _pdf_with

CII = "kosit-01.01a-INVOICE_uncefact.xml"
UBL_MANY = "kosit-01.13a-INVOICE_ubl.xml"


@pytest.fixture
def pdf_dir(app, tmp_path, monkeypatch):
    folder = tmp_path / "pdfs"
    folder.mkdir()
    monkeypatch.setitem(app.config, "PDF_DIR", str(folder))
    return folder


@pytest.fixture
def admin(app):
    user = User(username="admin", email="a@a.test", role_id=_ensure_role("Admin").id)
    user.set_password("secret")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def books(app):
    for year in (2015, 2016):
        db.session.add(FiscalYear(year=year, start_date=date(year, 1, 1), end_date=date(year, 12, 31)))
    account = Account(name="Wasserbezug", code="WBZ")
    db.session.add(account)
    db.session.commit()
    return account


def _login(client, username="admin"):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": username, "password": "secret"})


def _upload(client, *files, **extra):
    data = {"files": [(io.BytesIO(content), name) for name, content in files], **extra}
    return client.post("/accounting/documents/upload", data=data, content_type="multipart/form-data",
                       follow_redirects=False)


def _upload_json(client, name, content, **extra):
    data = {"file": (io.BytesIO(content), name), **extra}
    return client.post("/accounting/documents/upload", data=data, content_type="multipart/form-data",
                       headers={"Accept": "application/json"})


def _one(client):
    _upload(client, ("rechnung.xml", _fixture(CII)))
    return Document.query.one()


def _page(client, path):
    return client.get(path).get_data(as_text=True)


class TestAccess:
    def test_login_is_required(self, client):
        client.get("/auth/logout")
        assert client.get("/accounting/documents").status_code == 302

    def test_the_accounting_permission_is_required(self, client, admin):
        role = _ensure_role("Nur Stammdaten", perms=("stammdaten",))
        user = User(username="leser", email="l@a.test", role_id=role.id)
        user.set_password("secret")
        db.session.add(user)
        db.session.commit()
        _login(client, "leser")
        for path in ("/accounting/documents", "/accounting/documents/1", "/accounting/documents/panel/booking/1"):
            r = client.get(path)
            assert r.status_code == 302 and "/accounting/documents" not in r.headers["Location"]
        r = _upload(client, ("a.pdf", pdf()))
        assert r.status_code == 302 and Document.query.count() == 0

    def test_the_old_addresses_forward(self, client, admin, pdf_dir):
        _login(client)
        r = client.get("/accounting/incoming")
        assert r.status_code == 301 and r.headers["Location"].endswith("/accounting/documents")
        r = client.get("/accounting/incoming/7")
        assert r.status_code == 301 and r.headers["Location"].endswith("/accounting/documents/7")


class TestUpload:
    def test_a_single_file_opens_its_detail_page(self, client, admin, pdf_dir):
        _login(client)
        r = _upload(client, ("rechnung.xml", _fixture(CII)))
        doc = Document.query.one()
        assert r.status_code == 302 and r.headers["Location"].endswith(f"/accounting/documents/{doc.id}")
        assert doc.created_by_id == admin.id and storage.exists(doc.storage_key)

    def test_several_files_go_to_the_list(self, client, admin, pdf_dir):
        _login(client)
        r = _upload(client, ("a.xml", _fixture(CII)), ("b.xml", _fixture(UBL_MANY)))
        assert r.status_code == 302 and r.headers["Location"].endswith("/accounting/documents")
        assert Document.query.count() == 2
        html = _page(client, "/accounting/documents")
        assert "123456XX" in html and "2 Beleg(e) abgelegt" in html

    def test_an_unreadable_file_is_reported_per_file(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("kaputt.xml", b"kein xml"), ("gut.xml", _fixture(CII)))
        assert Document.query.count() == 1
        html = _page(client, "/accounting/documents")
        assert "kaputt.xml" in html and "konnte nicht abgelegt werden" in html

    def test_the_same_file_twice(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("a.xml", _fixture(CII)))
        r = _upload(client, ("b.xml", _fixture(CII)))
        assert Document.query.count() == 1
        assert "ist schon abgelegt" in client.get(r.headers["Location"]).get_data(as_text=True)

    def test_a_reissued_number_is_flagged_as_possible_duplicate(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("a.xml", _fixture(CII)))
        r = _upload(client, ("b.xml", _fixture(CII) + b"<!-- korrigiert -->"))
        assert "möglicherweise doppelt" in _page(client, r.headers["Location"])

    def test_no_file(self, client, admin, pdf_dir):
        _login(client)
        r = client.post("/accounting/documents/upload", data={}, follow_redirects=True)
        assert "Bitte mindestens eine Datei wählen" in r.get_data(as_text=True)

    def test_a_plain_pdf_is_now_accepted(self, client, admin, pdf_dir):
        _login(client)
        r = _upload(client, ("scan.pdf", _pdf_with(None)))
        doc = Document.query.one()
        assert doc.einvoice is None and doc.kind == "other" and r.headers["Location"].endswith(f"/documents/{doc.id}")
        html = _page(client, r.headers["Location"])
        assert "<iframe" in html and "Angaben zum Beleg" in html

    def test_a_photo_is_accepted(self, client, admin, pdf_dir):
        _login(client)
        r = _upload(client, ("bon.jpg", JPG))
        doc = Document.query.one()
        assert doc.content_type == "image/jpeg"
        assert f'src="/accounting/documents/{doc.id}/file"' in _page(client, r.headers["Location"])

    def test_an_unsupported_type_is_refused(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("makro.docx", b"PK\x03\x04" + b"\x00" * 30))
        assert Document.query.count() == 0
        assert "nicht unterstützt" in _page(client, "/accounting/documents")

    def test_json_for_fetch(self, client, admin, pdf_dir):
        _login(client)
        r = _upload_json(client, "a.pdf", pdf(1))
        body = r.get_json()
        doc = Document.query.one()
        assert r.status_code == 200 and body["ok"] and body["id"] == doc.id and not body["duplicate"]
        assert body["title"] == "a.pdf" and body["url"].endswith(f"/accounting/documents/{doc.id}")

    def test_json_errors_and_duplicates(self, client, admin, pdf_dir):
        _login(client)
        r = _upload_json(client, "x.gif", b"GIF89a" + b"\x00" * 30)
        assert r.status_code == 400 and not r.get_json()["ok"] and "nicht unterstützt" in r.get_json()["error"]
        _upload_json(client, "a.pdf", pdf(2))
        again = _upload_json(client, "b.pdf", pdf(2))
        assert again.status_code == 200 and again.get_json()["duplicate"] and again.get_json()["id"] == Document.query.one().id

    def test_an_upload_can_link_at_once(self, client, admin, pdf_dir, books):
        booking = Booking(date=date(2016, 4, 4), account_id=books.id, amount=Decimal("-5"), description="x")
        db.session.add(booking)
        db.session.commit()
        _login(client)
        r = _upload_json(client, "a.pdf", pdf(3), link_type="booking", link_id=str(booking.id))
        assert r.get_json()["linked"] is True and DocumentLink.query.one().booking_id == booking.id

    def test_a_duplicate_is_still_linked(self, client, admin, pdf_dir, books):
        booking = Booking(date=date(2016, 4, 4), account_id=books.id, amount=Decimal("-5"), description="x")
        db.session.add(booking)
        db.session.commit()
        _login(client)
        _upload_json(client, "a.pdf", pdf(4))
        r = _upload_json(client, "a.pdf", pdf(4), link_type="booking", link_id=str(booking.id))
        assert r.get_json()["duplicate"] and r.get_json()["linked"] and DocumentLink.query.count() == 1

    def test_the_shrink_note_is_logged(self, client, admin, pdf_dir):
        _login(client)
        _upload_json(client, "foto.jpg", JPG, shrunk="1", original_name="IMG_001.jpg", original_size="6000000")
        detail = DocumentEvent.query.filter_by(action="uploaded").one().detail_dict
        assert detail["verkleinert"] and detail["original_name"] == "IMG_001.jpg" and detail["original_groesse"] == 6_000_000

    def test_a_bad_link_target_is_a_404(self, client, admin, pdf_dir):
        _login(client)
        assert _upload_json(client, "a.pdf", pdf(5), link_type="customer", link_id="1").status_code == 404
        assert Document.query.count() == 0


class TestSizeLimit:
    def test_a_request_over_the_limit_is_stopped_early(self, client, admin, pdf_dir, app, monkeypatch):
        monkeypatch.setitem(app.config, "DOCUMENT_MAX_UPLOAD_MB", 1)
        _login(client)
        r = _upload_json(client, "gross.pdf", pdf() + b"0" * (3 * 1024 * 1024))
        assert r.status_code == 413 and "größer als 1 MB" in r.get_json()["error"]
        assert Document.query.count() == 0

    def test_a_form_post_gets_a_notice_instead(self, client, admin, pdf_dir, app, monkeypatch):
        monkeypatch.setitem(app.config, "DOCUMENT_MAX_UPLOAD_MB", 1)
        _login(client)
        r = _upload(client, ("gross.pdf", pdf() + b"0" * (3 * 1024 * 1024)))
        assert r.status_code == 302 and r.headers["Location"].endswith("/accounting/documents")
        assert "größer als 1 MB" in client.get(r.headers["Location"]).get_data(as_text=True)

    def test_a_file_between_the_limits_is_refused_by_the_service(self, client, admin, pdf_dir, app, monkeypatch):
        monkeypatch.setitem(app.config, "DOCUMENT_MAX_UPLOAD_MB", 1)
        _login(client)
        r = _upload_json(client, "knapp.pdf", pdf() + b"0" * (1024 * 1024 + 10))
        assert r.status_code == 400 and "größer als 1 MB" in r.get_json()["error"]

    def test_other_routes_have_no_such_limit(self, client, admin, pdf_dir, app, monkeypatch):
        monkeypatch.setitem(app.config, "DOCUMENT_MAX_UPLOAD_MB", 1)
        _login(client)
        # der Datenimport muss grosse ZIPs annehmen — die Grenze gilt nur fuer den Beleg-Upload
        r = client.post("/data-transfer/import", data={"file": (io.BytesIO(b"0" * (3 * 1024 * 1024)), "x.zip")},
                        content_type="multipart/form-data")
        assert r.status_code != 413


class TestList:
    def test_tabs_and_navigation(self, client, admin, pdf_dir):
        _login(client)
        doc = _one(client)
        assert "123456XX" in _page(client, "/accounting/documents")
        assert "123456XX" not in _page(client, "/accounting/documents?tab=booked")
        client.post(f"/accounting/documents/{doc.id}/discard")
        assert "123456XX" not in _page(client, "/accounting/documents")
        assert "123456XX" in _page(client, "/accounting/documents?tab=discarded")
        assert "123456XX" in _page(client, "/accounting/documents?tab=all")
        html = _page(client, "/accounting/documents")
        assert 'href="/accounting/documents"' in html and "Belege" in html           # Seitenleiste

    def test_the_search(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("a.xml", _fixture(CII)), ("lieferschein-hanf.pdf", pdf(6)))
        html = _page(client, "/accounting/documents?q=hanf")
        assert "lieferschein-hanf.pdf" in html and "123456XX" not in html
        assert "123456XX" in _page(client, "/accounting/documents?q=123456")
        assert "Keine Belege zur Suche" in _page(client, "/accounting/documents?q=gibtsnicht")

    def test_an_unknown_tab_falls_back(self, client, admin, pdf_dir):
        _login(client)
        assert client.get("/accounting/documents?tab=quatsch").status_code == 200

    def test_the_storage_use_is_shown(self, client, admin, pdf_dir, app, monkeypatch):
        monkeypatch.setitem(app.config, "DOCUMENT_QUOTA_MB", 2048)
        _login(client)
        _upload(client, ("a.pdf", pdf(7)))
        assert "von 2,0 GB" in _page(client, "/accounting/documents")


class TestDetail:
    def test_the_invoice_is_shown_readably(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        html = _page(client, f"/accounting/documents/{doc.id}")
        for expected in ("123456XX", "04.04.2016", "[Seller name]", "DE123456789", "XRechnung (XML)",
                         "336,90", "Positionen", "Summen", "Es wird gebucht", "Aufbewahren bis"):
            assert expected in html, expected
        assert "Neu anlegen: [Seller name]" in html              # Lieferant noch unbekannt

    def test_a_known_supplier_is_preselected(self, client, admin, pdf_dir, books):
        supplier = Customer(name="Lieferant GmbH", is_supplier=True, is_customer=False, vat_id="DE123456789")
        db.session.add(supplier)
        db.session.commit()
        _login(client)
        doc = _one(client)
        html = _page(client, f"/accounting/documents/{doc.id}")
        assert f'<option value="{supplier.id}" selected>' in html and "Erkannt über die USt-IdNr." in html

    def test_a_pdf_hybrid_is_shown_the_same_way(self, client, admin, pdf_dir, books):
        _login(client)
        _upload(client, ("lieferant.pdf", _pdf_with(_fixture(CII))))
        doc = Document.query.one()
        html = _page(client, f"/accounting/documents/{doc.id}")
        assert "ZUGFeRD / Factur-X (PDF)" in html and "Original ansehen" in html and "123456XX" in html

    def test_hostile_text_in_the_invoice_is_escaped(self, client, admin, pdf_dir, books):
        evil = _fixture(CII).replace(b"[Seller name]", b"&lt;script&gt;alert(1)&lt;/script&gt;")
        _login(client)
        _upload(client, ("boese.xml", evil))
        doc = Document.query.one()
        html = _page(client, f"/accounting/documents/{doc.id}")
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_a_hostile_file_name_is_escaped(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("<img src=x onerror=alert(1)>.pdf", pdf(8)))
        doc = Document.query.one()
        for path in (f"/accounting/documents/{doc.id}", "/accounting/documents"):
            assert "<img src=x onerror" not in _page(client, path)

    def test_a_missing_file_is_reported(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        storage.delete(doc.storage_key)
        assert "Datei fehlt" in _page(client, f"/accounting/documents/{doc.id}")
        r = client.get(f"/accounting/documents/{doc.id}/file", follow_redirects=True)
        assert "nicht mehr vorhanden" in r.get_data(as_text=True)

    def test_unknown_document(self, client, admin):
        _login(client)
        assert client.get("/accounting/documents/9999").status_code == 404

    def test_a_plain_document_has_a_metadata_form_an_einvoice_has_not(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("a.pdf", pdf(9)), ("r.xml", _fixture(CII)))
        plain = Document.query.filter(Document.einvoice == None).one()      # noqa: E711
        einvoice = Document.query.filter(Document.einvoice != None).one()   # noqa: E711
        assert "Angaben zum Beleg" in _page(client, f"/accounting/documents/{plain.id}")
        assert "Angaben zum Beleg" not in _page(client, f"/accounting/documents/{einvoice.id}")

    def test_the_history_is_shown(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("a.pdf", pdf(10)))
        doc = Document.query.one()
        client.post(f"/accounting/documents/{doc.id}/shelve")
        html = _page(client, f"/accounting/documents/{doc.id}")
        assert "ohne Buchung abgelegt" in html and "abgelegt" in html and "admin" in html


class TestDownloads:
    def test_the_original_xml_is_served_unchanged_as_a_download(self, client, admin, pdf_dir):
        _login(client)
        doc = _one(client)
        r = client.get(f"/accounting/documents/{doc.id}/file")
        assert r.data == _fixture(CII) and "attachment" in r.headers["Content-Disposition"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_the_original_pdf_is_shown_inline(self, client, admin, pdf_dir):
        data = _pdf_with(_fixture(CII))
        _login(client)
        _upload(client, ("l.pdf", data))
        doc = Document.query.one()
        r = client.get(f"/accounting/documents/{doc.id}/file")
        assert r.data == data and r.mimetype == "application/pdf"
        assert "attachment" not in r.headers.get("Content-Disposition", "")
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_a_photo_is_shown_inline_with_its_real_type(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("bon.jpg", JPG))
        doc = Document.query.one()
        r = client.get(f"/accounting/documents/{doc.id}/file")
        assert r.data == JPG and r.mimetype == "image/jpeg"

    def test_download_forces_an_attachment(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("a.pdf", pdf(11)))
        doc = Document.query.one()
        r = client.get(f"/accounting/documents/{doc.id}/file?download=1")
        assert "attachment" in r.headers["Content-Disposition"] and "a.pdf" in r.headers["Content-Disposition"]

    def test_the_stored_file_name_never_decides_the_type(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("harmlos.html", pdf(12)))
        doc = Document.query.one()
        r = client.get(f"/accounting/documents/{doc.id}/file")
        assert r.mimetype == "application/pdf"

    def test_the_xml_of_a_hybrid_pdf_can_be_downloaded(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("l.pdf", _pdf_with(_fixture(CII))))
        doc = Document.query.one()
        r = client.get(f"/accounting/documents/{doc.id}/xml")
        assert r.data == _fixture(CII) and 'filename="123456XX.xml"' in r.headers["Content-Disposition"]

    def test_a_plain_document_has_no_einvoice_xml(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("a.pdf", pdf(13)))
        doc = Document.query.one()
        assert client.get(f"/accounting/documents/{doc.id}/xml").status_code == 404
        assert client.get(f"/accounting/documents/{doc.id}/attachment/0").status_code == 404

    def _with_attachment(self, client, content, mime="application/pdf"):
        xml = _fixture(CII).replace(
            b"</ram:ApplicableHeaderTradeAgreement>",
            b'<ram:AdditionalReferencedDocument><ram:IssuerAssignedID>a</ram:IssuerAssignedID>'
            b'<ram:TypeCode>916</ram:TypeCode><ram:AttachmentBinaryObject mimeCode="' + mime.encode() +
            b'" filename="anhang.pdf">' + base64.b64encode(content) + b'</ram:AttachmentBinaryObject>'
            b'</ram:AdditionalReferencedDocument></ram:ApplicableHeaderTradeAgreement>', 1)
        _upload(client, ("mit-anhang.xml", xml))
        return Document.query.one()

    def test_an_embedded_pdf_is_shown_inline(self, client, admin, pdf_dir, books):
        _login(client)
        doc = self._with_attachment(client, b"%PDF-1.7 ansicht")
        assert "anhang.pdf" in _page(client, f"/accounting/documents/{doc.id}")
        r = client.get(f"/accounting/documents/{doc.id}/attachment/0")
        assert r.data == b"%PDF-1.7 ansicht" and r.mimetype == "application/pdf"
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_an_attachment_that_only_claims_to_be_a_pdf_is_downloaded(self, client, admin, pdf_dir, books):
        """Die Art gibt der Absender an — der Inhalt muss stimmen, sonst nie inline."""
        _login(client)
        doc = self._with_attachment(client, b"<html><script>alert(1)</script></html>")
        r = client.get(f"/accounting/documents/{doc.id}/attachment/0")
        assert r.mimetype == "application/octet-stream" and "attachment" in r.headers["Content-Disposition"]

    def test_unknown_attachment_index(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        assert client.get(f"/accounting/documents/{doc.id}/attachment/3").status_code == 404


class TestBookingAnEinvoice:
    def _book(self, client, doc, account, **extra):
        data = {"supplier_id": "new", "date": "2016-04-04", "account_id": str(account.id),
                "project_id": "", "real_account_id": "", **extra}
        return client.post(f"/accounting/documents/{doc.id}/book", data=data, follow_redirects=False)

    def test_a_single_rate_is_booked_and_the_supplier_created(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        r = self._book(client, doc, books)
        assert r.status_code == 302 and r.headers["Location"].endswith(f"/accounting/documents/{doc.id}")
        doc = db.session.get(Document, doc.id)
        link = DocumentLink.query.one()
        booking = db.session.get(Booking, link.booking_id)
        assert svc.is_booked(doc) and booking.amount == Decimal("-336.90")
        supplier = db.session.get(Customer, doc.supplier_id)
        assert supplier.is_supplier and supplier.name == "[Seller name]" and supplier.vat_id == "DE123456789"
        html = _page(client, r.headers["Location"])
        assert "Verbucht" in html and "Buchung aus diesem Beleg" not in html

    def test_several_rates_end_on_the_group_page(self, client, admin, pdf_dir, books):
        _login(client)
        _upload(client, ("viele.xml", _fixture(UBL_MANY)))
        doc = Document.query.one()
        parsed = doc.einvoice.parsed
        r = self._book(client, doc, books, date="2015-01-09")
        link = DocumentLink.query.one()
        if len(parsed.vat) > 1:
            assert link.booking_group_id and f"/booking-groups/{link.booking_group_id}/edit" in r.headers["Location"]
            assert BookingGroup.query.one().total_amount == -parsed.grand_total
        else:
            assert link.booking_id

    def test_an_existing_supplier_is_used(self, client, admin, pdf_dir, books):
        supplier = Customer(name="Lieferant GmbH", is_supplier=True, is_customer=False)
        db.session.add(supplier)
        db.session.commit()
        _login(client)
        doc = _one(client)
        self._book(client, doc, books, supplier_id=str(supplier.id))
        assert Customer.query.count() == 1 and db.session.get(Document, doc.id).supplier_id == supplier.id

    def test_a_customer_cannot_be_chosen_as_supplier(self, client, admin, pdf_dir, books):
        customer = Customer(name="Nur Kunde", is_supplier=False, is_customer=True)
        db.session.add(customer)
        db.session.commit()
        _login(client)
        doc = _one(client)
        r = self._book(client, doc, books, supplier_id=str(customer.id))
        assert "Lieferanten" in client.get(r.headers["Location"]).get_data(as_text=True)
        assert not svc.is_booked(db.session.get(Document, doc.id))

    @pytest.mark.parametrize("extra, message", [
        ({"account_id": ""}, "Konto"),
        ({"date": "2999-01-01"}, "Zukunft"),
        ({"date": "2010-01-01"}, "Buchungsjahr"),
        ({"date": "kein-datum"}, "Ungültiges Buchungsdatum"),
    ])
    def test_refusals_are_shown_and_nothing_is_created(self, client, admin, pdf_dir, books, extra, message):
        _login(client)
        doc = _one(client)
        data = {"supplier_id": "new", "date": "2016-04-04", "account_id": str(books.id)}
        data.update(extra)
        r = client.post(f"/accounting/documents/{doc.id}/book", data=data, follow_redirects=True)
        assert message in r.get_data(as_text=True)
        assert Booking.query.count() == 0 and DocumentLink.query.count() == 0
        assert Customer.query.count() == 0                        # auch der neue Lieferant ist zurückgerollt

    def test_booking_twice_is_refused(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        self._book(client, doc, books)
        r = client.post(f"/accounting/documents/{doc.id}/book", data={
            "supplier_id": "new", "date": "2016-04-04", "account_id": str(books.id)}, follow_redirects=True)
        assert "bereits verbucht" in r.get_data(as_text=True) and Booking.query.count() == 1

    def test_a_plain_document_has_no_booking_suggestion(self, client, admin, pdf_dir, books):
        _login(client)
        _upload(client, ("a.pdf", pdf(14)))
        doc = Document.query.one()
        assert client.post(f"/accounting/documents/{doc.id}/book", data={}).status_code == 404


class TestWorkflow:
    def test_discard_keeps_the_file_and_can_be_reopened(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        key = doc.storage_key
        r = client.post(f"/accounting/documents/{doc.id}/discard", follow_redirects=True)
        assert "Die Datei bleibt aufbewahrt" in r.get_data(as_text=True)
        assert db.session.get(Document, doc.id).status == "Verworfen" and storage.exists(key)
        client.post(f"/accounting/documents/{doc.id}/reopen")
        assert db.session.get(Document, doc.id).status == "Neu"

    def test_a_booked_document_cannot_be_discarded(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        client.post(f"/accounting/documents/{doc.id}/book", data={
            "supplier_id": "new", "date": "2016-04-04", "account_id": str(books.id)})
        r = client.post(f"/accounting/documents/{doc.id}/discard", follow_redirects=True)
        assert "nicht verwerfen" in r.get_data(as_text=True)
        assert db.session.get(Document, doc.id).status == "Neu"

    def test_shelving(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("vertrag.pdf", pdf(15)))
        doc = Document.query.one()
        client.post(f"/accounting/documents/{doc.id}/shelve")
        assert db.session.get(Document, doc.id).status == "Abgelegt"
        assert "vertrag.pdf" in _page(client, "/accounting/documents?tab=filed")
        assert "vertrag.pdf" not in _page(client, "/accounting/documents")

    def test_delete_a_never_linked_document(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("falsch.pdf", pdf(16)))
        doc = Document.query.one()
        key = doc.storage_key
        r = client.post(f"/accounting/documents/{doc.id}/delete", follow_redirects=True)
        assert "Beleg gelöscht" in r.get_data(as_text=True)
        assert Document.query.count() == 0 and not storage.exists(key)

    def test_a_filed_document_cannot_be_deleted(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("vertrag.pdf", pdf(17)))
        doc = Document.query.one()
        client.post(f"/accounting/documents/{doc.id}/shelve")
        r = client.post(f"/accounting/documents/{doc.id}/delete", follow_redirects=True)
        assert "aufbewahrungspflichtig" in r.get_data(as_text=True) and Document.query.count() == 1
        assert "Nicht löschbar" in _page(client, f"/accounting/documents/{doc.id}")

    def test_update_the_metadata(self, client, admin, pdf_dir):
        supplier = Customer(name="Baumarkt", is_supplier=True, is_customer=False)
        db.session.add(supplier)
        db.session.commit()
        _login(client)
        _upload(client, ("bon.jpg", JPG))
        doc = Document.query.one()
        r = client.post(f"/accounting/documents/{doc.id}/update", data={
            "title": "Kassenbon Baumarkt", "number": "B-17", "document_date": "2026-05-01", "amount": "23,40",
            "supplier_id": str(supplier.id), "kind": "receipt"}, follow_redirects=True)
        assert "Gespeichert" in r.get_data(as_text=True)
        doc = db.session.get(Document, doc.id)
        assert (doc.title, doc.number, doc.amount, doc.kind) == ("Kassenbon Baumarkt", "B-17", Decimal("23.40"), "receipt")
        assert doc.supplier_id == supplier.id and doc.document_date == date(2026, 5, 1)

    @pytest.mark.parametrize("field, value, message", [
        ("document_date", "morgen", "Ungültiges Belegdatum"),
        ("amount", "viel", "Ungültiger Betrag"),
        ("kind", "spam", "Ungültige Belegart"),
    ])
    def test_invalid_metadata_is_refused(self, client, admin, pdf_dir, field, value, message):
        _login(client)
        _upload(client, ("bon.jpg", JPG))
        doc = Document.query.one()
        data = {"title": "", "number": "", "document_date": "", "amount": "", "supplier_id": "", "kind": "other"}
        data[field] = value
        r = client.post(f"/accounting/documents/{doc.id}/update", data=data, follow_redirects=True)
        assert message in r.get_data(as_text=True)

    def test_link_to_an_existing_booking(self, client, admin, pdf_dir, books):
        booking = Booking(date=date(2016, 4, 4), account_id=books.id, amount=Decimal("-336.90"), description="Bezug")
        db.session.add(booking)
        db.session.commit()
        _login(client)
        doc = _one(client)
        html = _page(client, f"/accounting/documents/{doc.id}")
        assert "Passend (gleicher Betrag, ohne Beleg)" in html and f'value="booking:{booking.id}"' in html
        r = client.post(f"/accounting/documents/{doc.id}/link", data={"target": f"booking:{booking.id}"}, follow_redirects=True)
        assert "Beleg der Buchung zugeordnet" in r.get_data(as_text=True)
        assert DocumentLink.query.one().booking_id == booking.id and svc.is_booked(db.session.get(Document, doc.id))

    def test_link_to_a_group(self, client, admin, pdf_dir, books):
        group = BookingGroup(date=date(2016, 4, 4), description="Sammel", total_amount=Decimal("-10"), status="Aktiv")
        db.session.add(group)
        db.session.commit()
        _login(client)
        _upload(client, ("a.pdf", pdf(18)))
        doc = Document.query.one()
        client.post(f"/accounting/documents/{doc.id}/link", data={"target": f"group:{group.id}"})
        assert DocumentLink.query.one().booking_group_id == group.id

    @pytest.mark.parametrize("target", ["", "booking:", "booking:abc", "customer:1", "booking:9999"])
    def test_a_bad_link_target(self, client, admin, pdf_dir, target):
        _login(client)
        _upload(client, ("a.pdf", pdf(19)))
        doc = Document.query.one()
        r = client.post(f"/accounting/documents/{doc.id}/link", data={"target": target})
        assert r.status_code in (302, 404) and DocumentLink.query.count() == 0

    def test_unlink_from_the_document_page(self, client, admin, pdf_dir, books):
        booking = Booking(date=date(2016, 4, 4), account_id=books.id, amount=Decimal("-336.90"), description="Bezug")
        db.session.add(booking)
        db.session.commit()
        _login(client)
        doc = _one(client)
        client.post(f"/accounting/documents/{doc.id}/link", data={"target": f"booking:{booking.id}"})
        link = DocumentLink.query.one()
        r = client.post(f"/accounting/documents/links/{link.id}/unlink", follow_redirects=True)
        assert "wieder im Eingang" in r.get_data(as_text=True) and DocumentLink.query.count() == 0

    def test_unlink_in_a_closed_year_is_refused(self, client, admin, pdf_dir, books):
        booking = Booking(date=date(2016, 4, 4), account_id=books.id, amount=Decimal("-336.90"), description="Bezug")
        db.session.add(booking)
        db.session.commit()
        _login(client)
        doc = _one(client)
        client.post(f"/accounting/documents/{doc.id}/link", data={"target": f"booking:{booking.id}"})
        FiscalYear.query.filter_by(year=2016).one().closed = True
        db.session.commit()
        link = DocumentLink.query.one()
        assert "Das Buchungsjahr 2016 ist abgeschlossen" in _page(client, f"/accounting/documents/{doc.id}")
        r = client.post(f"/accounting/documents/links/{link.id}/unlink", follow_redirects=True)
        assert "abgeschlossen" in r.get_data(as_text=True) and DocumentLink.query.count() == 1


class TestBookingLink:
    """Von der Buchung zurück zum Beleg; Löschen gibt den Beleg frei, ein Storno lässt den Nachweis stehen."""

    def _booked(self, client, books, date_="2016-04-04"):
        _upload(client, ("r.xml", _fixture(CII)))
        doc = Document.query.one()
        client.post(f"/accounting/documents/{doc.id}/book", data={
            "supplier_id": "new", "date": date_, "account_id": str(books.id)})
        return db.session.get(Document, doc.id), DocumentLink.query.one()

    def _two_rates(self, client, books, date_="2016-04-04"):
        from app.einvoice.incoming import InVat
        _upload(client, ("r.xml", _fixture(CII)))
        doc = Document.query.one()
        parsed = doc.einvoice.parsed
        parsed.vat = [InVat("S", Decimal("19"), Decimal("200"), Decimal("38")),
                      InVat("S", Decimal("7"), Decimal("100"), Decimal("7"))]
        parsed.grand_total = Decimal("345")
        doc.einvoice.data = parsed.to_json()
        db.session.commit()
        client.post(f"/accounting/documents/{doc.id}/book", data={
            "supplier_id": "new", "date": date_, "account_id": str(books.id)})
        return db.session.get(Document, doc.id), DocumentLink.query.one()

    def test_the_booking_page_lists_the_document(self, client, admin, pdf_dir, books):
        _login(client)
        doc, link = self._booked(client, books)
        html = _page(client, f"/accounting/bookings/{link.booking_id}/edit")
        assert "[Seller name]" in html and f"/accounting/documents/{doc.id}" in html and "zugeordnet" in html

    def test_the_group_page_lists_the_document(self, client, admin, pdf_dir, books):
        _login(client)
        doc, link = self._two_rates(client, books)
        assert link.booking_group_id
        html = _page(client, f"/accounting/booking-groups/{link.booking_group_id}/edit")
        assert "[Seller name]" in html and f"/accounting/documents/{doc.id}" in html

    def test_an_ordinary_booking_shows_no_documents(self, client, admin, pdf_dir, books):
        booking = Booking(date=date(2016, 1, 5), account_id=books.id, amount=Decimal("-10"), description="x")
        db.session.add(booking)
        db.session.commit()
        _login(client)
        assert "zugeordnet" not in _page(client, f"/accounting/bookings/{booking.id}/edit")

    def test_deleting_the_booking_returns_the_document_to_the_inbox(self, client, admin, pdf_dir, books):
        _login(client)
        doc, link = self._booked(client, books)
        r = client.post(f"/accounting/bookings/{link.booking_id}/delete", follow_redirects=True)
        assert "Die Belege sind wieder im Belegeingang" in r.get_data(as_text=True)
        doc = db.session.get(Document, doc.id)
        assert not svc.is_booked(doc) and DocumentLink.query.count() == 0 and Booking.query.count() == 0
        assert doc.id in {d.id for d in Document.query.filter(svc.tab_filter("inbox"))}
        assert "123456XX" in _page(client, "/accounting/documents")

    def test_deleting_one_booking_keeps_a_document_that_is_still_linked_elsewhere(self, client, admin, pdf_dir, books):
        _login(client)
        doc, link = self._booked(client, books)
        other = Booking(date=date(2016, 5, 5), account_id=books.id, amount=Decimal("-10"), description="zweite")
        db.session.add(other)
        db.session.flush()
        svc.link(doc, booking=other)
        db.session.commit()
        r = client.post(f"/accounting/bookings/{link.booking_id}/delete", follow_redirects=True)
        text = r.get_data(as_text=True)
        assert "weiterhin anderen Buchungen zugeordnet" in text
        assert "Die Belege sind wieder im Belegeingang" not in text
        doc = db.session.get(Document, doc.id)
        assert svc.is_booked(doc) and DocumentLink.query.count() == 1

    def test_deleting_the_group_returns_the_document_to_the_inbox(self, client, admin, pdf_dir, books):
        _login(client)
        doc, link = self._two_rates(client, books)
        client.post(f"/accounting/booking-groups/{link.booking_group_id}/delete")
        assert not svc.is_booked(db.session.get(Document, doc.id)) and DocumentLink.query.count() == 0
        assert BookingGroup.query.count() == 0 and Booking.query.count() == 0

    def test_the_returned_document_can_be_booked_again(self, client, admin, pdf_dir, books):
        _login(client)
        doc, link = self._booked(client, books)
        client.post(f"/accounting/bookings/{link.booking_id}/delete")
        r = client.post(f"/accounting/documents/{doc.id}/book", data={
            "supplier_id": str(db.session.get(Document, doc.id).supplier_id or "new"),
            "date": "2016-04-04", "account_id": str(books.id)}, follow_redirects=True)
        assert svc.is_booked(db.session.get(Document, doc.id)) and Booking.query.count() == 1
        assert r.status_code == 200

    def _today_books(self):
        year = date.today().year
        if FiscalYear.query.filter_by(year=year).first() is None:
            db.session.add(FiscalYear(year=year, start_date=date(year, 1, 1), end_date=date(year, 12, 31)))
            db.session.commit()

    def test_storno_of_the_booking_keeps_the_link_and_reopens_the_document(self, client, admin, pdf_dir, books):
        self._today_books()
        _login(client)
        doc, link = self._booked(client, books, date_=date.today().isoformat())
        booking_id = link.booking_id
        client.post(f"/accounting/bookings/{booking_id}/stornieren", data={"storno_reason": "falsch erfasst"})
        assert db.session.get(Booking, booking_id).status == Booking.STATUS_STORNIERT
        doc = db.session.get(Document, doc.id)
        assert not svc.is_booked(doc) and DocumentLink.query.count() == 1            # Nachweis bleibt
        assert doc.id in {d.id for d in Document.query.filter(svc.tab_filter("inbox"))}

    def test_storno_of_the_group_keeps_the_link_and_reopens_the_document(self, client, admin, pdf_dir, books):
        self._today_books()
        _login(client)
        doc, link = self._two_rates(client, books, date_=date.today().isoformat())
        client.post(f"/accounting/booking-groups/{link.booking_group_id}/stornieren", data={"storno_reason": "doppelt"})
        assert db.session.get(BookingGroup, link.booking_group_id).status == BookingGroup.STATUS_STORNIERT
        assert not svc.is_booked(db.session.get(Document, doc.id)) and DocumentLink.query.count() == 1

    def test_a_customer_with_documents_cannot_be_deleted(self, client, admin, pdf_dir, books):
        _login(client)
        doc, _link = self._booked(client, books)
        supplier_id = db.session.get(Document, doc.id).supplier_id
        r = client.post(f"/customers/{supplier_id}/delete", follow_redirects=True)
        assert "Belege" in r.get_data(as_text=True) and db.session.get(Customer, supplier_id) is not None
