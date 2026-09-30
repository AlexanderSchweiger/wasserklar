"""Eingangsrechnungen (empfangene E-Rechnungen) in der Oberfläche."""
import io
import os
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, FiscalYear, IncomingInvoice, User,
)
from tests.conftest import _ensure_role
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


def _upload(client, *files):
    data = {"files": [(io.BytesIO(content), name) for name, content in files]}
    return client.post("/accounting/incoming/upload", data=data, content_type="multipart/form-data",
                       follow_redirects=False)


def _one(client):
    _upload(client, ("rechnung.xml", _fixture(CII)))
    return IncomingInvoice.query.one()


class TestAccess:
    def test_login_is_required(self, client):
        client.get("/auth/logout")
        assert client.get("/accounting/incoming").status_code == 302

    def test_the_accounting_permission_is_required(self, client, admin):
        role = _ensure_role("Nur Stammdaten", perms=("stammdaten",))
        user = User(username="leser", email="l@a.test", role_id=role.id)
        user.set_password("secret")
        db.session.add(user)
        db.session.commit()
        _login(client, "leser")
        r = client.get("/accounting/incoming")
        assert r.status_code == 302 and "/accounting/incoming" not in r.headers["Location"]


class TestUpload:
    def test_a_single_file_opens_its_detail_page(self, client, admin, pdf_dir):
        _login(client)
        r = _upload(client, ("rechnung.xml", _fixture(CII)))
        doc = IncomingInvoice.query.one()
        assert r.status_code == 302 and r.headers["Location"].endswith(f"/accounting/incoming/{doc.id}")
        assert doc.created_by_id == admin.id and os.path.exists(doc.file_path)

    def test_several_files_go_to_the_list(self, client, admin, pdf_dir):
        _login(client)
        r = _upload(client, ("a.xml", _fixture(CII)), ("b.xml", _fixture(UBL_MANY)))
        assert r.status_code == 302 and r.headers["Location"].endswith("/accounting/incoming")
        assert IncomingInvoice.query.count() == 2
        html = client.get("/accounting/incoming").get_data(as_text=True)
        assert "123456XX" in html and "Rechnungsnummer" in html

    def test_an_unreadable_file_is_reported_per_file(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("kaputt.xml", b"kein xml"), ("gut.xml", _fixture(CII)))
        assert IncomingInvoice.query.count() == 1
        html = client.get("/accounting/incoming").get_data(as_text=True)
        assert "kaputt.xml" in html and "konnte nicht gelesen werden" in html

    def test_the_same_file_twice(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("a.xml", _fixture(CII)))
        r = _upload(client, ("b.xml", _fixture(CII)))
        assert IncomingInvoice.query.count() == 1
        assert "ist schon erfasst" in client.get(r.headers["Location"]).get_data(as_text=True)

    def test_a_reissued_number_is_flagged_as_possible_duplicate(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("a.xml", _fixture(CII)))
        r = _upload(client, ("b.xml", _fixture(CII) + b"<!-- korrigiert -->"))
        html = client.get(r.headers["Location"]).get_data(as_text=True)
        assert "möglicherweise doppelt" in html

    def test_no_file(self, client, admin, pdf_dir):
        _login(client)
        r = client.post("/accounting/incoming/upload", data={}, follow_redirects=True)
        assert "Bitte mindestens eine Datei wählen" in r.get_data(as_text=True)

    def test_a_pdf_without_einvoice_data_is_explained(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("scan.pdf", _pdf_with(None)))
        html = client.get("/accounting/incoming").get_data(as_text=True)
        assert "keine E-Rechnungsdaten" in html and IncomingInvoice.query.count() == 0


class TestList:
    def test_filters_and_navigation(self, client, admin, pdf_dir):
        _login(client)
        doc = _one(client)
        assert "123456XX" in client.get("/accounting/incoming").get_data(as_text=True)
        assert "123456XX" not in client.get("/accounting/incoming?status=booked").get_data(as_text=True)
        client.post(f"/accounting/incoming/{doc.id}/discard")
        assert "123456XX" in client.get("/accounting/incoming?status=discarded").get_data(as_text=True)
        assert "123456XX" in client.get("/accounting/incoming?status=all").get_data(as_text=True)
        html = client.get("/accounting/incoming").get_data(as_text=True)
        assert 'href="/accounting/incoming"' in html          # Eintrag in der Seitenleiste

    def test_an_unknown_filter_falls_back(self, client, admin, pdf_dir):
        _login(client)
        assert client.get("/accounting/incoming?status=quatsch").status_code == 200


class TestDetail:
    def test_the_invoice_is_shown_readably(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        html = client.get(f"/accounting/incoming/{doc.id}").get_data(as_text=True)
        for expected in ("123456XX", "04.04.2016", "[Seller name]", "DE123456789", "XRechnung (XML)",
                         "336,90", "Positionen", "Summen", "Es wird gebucht"):
            assert expected in html, expected
        assert "Neu anlegen: [Seller name]" in html              # Lieferant noch unbekannt

    def test_a_known_supplier_is_preselected(self, client, admin, pdf_dir, books):
        supplier = Customer(name="Lieferant GmbH", is_supplier=True, is_customer=False, vat_id="DE123456789")
        db.session.add(supplier)
        db.session.commit()
        _login(client)
        doc = _one(client)
        html = client.get(f"/accounting/incoming/{doc.id}").get_data(as_text=True)
        assert f'<option value="{supplier.id}" selected>' in html and "Erkannt über die USt-IdNr." in html

    def test_a_pdf_hybrid_is_shown_the_same_way(self, client, admin, pdf_dir, books):
        _login(client)
        _upload(client, ("lieferant.pdf", _pdf_with(_fixture(CII))))
        doc = IncomingInvoice.query.one()
        html = client.get(f"/accounting/incoming/{doc.id}").get_data(as_text=True)
        assert "ZUGFeRD / Factur-X (PDF)" in html and "Original ansehen" in html and "123456XX" in html

    def test_hostile_text_in_the_invoice_is_escaped(self, client, admin, pdf_dir, books):
        # Als Text (mit XML-Entities) im Namen des Lieferanten, nicht als Element.
        evil = _fixture(CII).replace(b"[Seller name]", b"&lt;script&gt;alert(1)&lt;/script&gt;")
        _login(client)
        _upload(client, ("boese.xml", evil))
        doc = IncomingInvoice.query.one()
        html = client.get(f"/accounting/incoming/{doc.id}").get_data(as_text=True)
        assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_a_missing_original_is_reported(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        os.remove(doc.file_path)
        html = client.get(f"/accounting/incoming/{doc.id}").get_data(as_text=True)
        assert "Original fehlt" in html
        r = client.get(f"/accounting/incoming/{doc.id}/file", follow_redirects=True)
        assert "nicht mehr vorhanden" in r.get_data(as_text=True)

    def test_unknown_document(self, client, admin):
        _login(client)
        assert client.get("/accounting/incoming/9999").status_code == 404


class TestDownloads:
    def test_the_original_xml_is_served_unchanged_as_a_download(self, client, admin, pdf_dir):
        _login(client)
        doc = _one(client)
        r = client.get(f"/accounting/incoming/{doc.id}/file")
        assert r.data == _fixture(CII) and "attachment" in r.headers["Content-Disposition"]

    def test_the_original_pdf_is_shown_inline(self, client, admin, pdf_dir):
        pdf = _pdf_with(_fixture(CII))
        _login(client)
        _upload(client, ("l.pdf", pdf))
        doc = IncomingInvoice.query.one()
        r = client.get(f"/accounting/incoming/{doc.id}/file")
        assert r.data == pdf and r.mimetype == "application/pdf"
        assert "attachment" not in r.headers.get("Content-Disposition", "")

    def test_the_xml_of_a_hybrid_pdf_can_be_downloaded(self, client, admin, pdf_dir):
        _login(client)
        _upload(client, ("l.pdf", _pdf_with(_fixture(CII))))
        doc = IncomingInvoice.query.one()
        r = client.get(f"/accounting/incoming/{doc.id}/xml")
        assert r.data == _fixture(CII) and 'filename="123456XX.xml"' in r.headers["Content-Disposition"]

    def _with_attachment(self, client, content, mime="application/pdf"):
        import base64
        xml = _fixture(CII).replace(
            b"</ram:ApplicableHeaderTradeAgreement>",
            b'<ram:AdditionalReferencedDocument><ram:IssuerAssignedID>a</ram:IssuerAssignedID>'
            b'<ram:TypeCode>916</ram:TypeCode><ram:AttachmentBinaryObject mimeCode="' + mime.encode() +
            b'" filename="anhang.pdf">' + base64.b64encode(content) + b'</ram:AttachmentBinaryObject>'
            b'</ram:AdditionalReferencedDocument></ram:ApplicableHeaderTradeAgreement>', 1)
        _upload(client, ("mit-anhang.xml", xml))
        return IncomingInvoice.query.one()

    def test_an_embedded_pdf_is_shown_inline(self, client, admin, pdf_dir, books):
        _login(client)
        doc = self._with_attachment(client, b"%PDF-1.7 ansicht")
        assert "anhang.pdf" in client.get(f"/accounting/incoming/{doc.id}").get_data(as_text=True)
        r = client.get(f"/accounting/incoming/{doc.id}/attachment/0")
        assert r.data == b"%PDF-1.7 ansicht" and r.mimetype == "application/pdf"
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_an_attachment_that_only_claims_to_be_a_pdf_is_downloaded(self, client, admin, pdf_dir, books):
        """Die Art gibt der Absender an — der Inhalt muss stimmen, sonst nie inline."""
        _login(client)
        doc = self._with_attachment(client, b"<html><script>alert(1)</script></html>")
        r = client.get(f"/accounting/incoming/{doc.id}/attachment/0")
        assert r.mimetype == "application/octet-stream" and "attachment" in r.headers["Content-Disposition"]

    def test_unknown_attachment_index(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        assert client.get(f"/accounting/incoming/{doc.id}/attachment/3").status_code == 404


class TestBooking:
    def _book(self, client, doc, account, **extra):
        data = {"supplier_id": "new", "date": "2016-04-04", "account_id": str(account.id),
                "project_id": "", "real_account_id": "", **extra}
        return client.post(f"/accounting/incoming/{doc.id}/book", data=data, follow_redirects=False)

    def test_a_single_rate_is_booked_and_the_supplier_created(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        r = self._book(client, doc, books)
        assert r.status_code == 302 and r.headers["Location"].endswith(f"/accounting/incoming/{doc.id}")
        doc = db.session.get(IncomingInvoice, doc.id)
        booking = db.session.get(Booking, doc.booking_id)
        assert doc.status == "Verbucht" and booking.amount == Decimal("-336.90")
        supplier = db.session.get(Customer, doc.supplier_id)
        assert supplier.is_supplier and supplier.name == "[Seller name]" and supplier.vat_id == "DE123456789"
        html = client.get(r.headers["Location"]).get_data(as_text=True)
        assert "Buchung ansehen" in html and "Verbucht" in html and "Gebucht" not in html

    def test_several_rates_end_on_the_group_page(self, client, admin, pdf_dir, books):
        _login(client)
        _upload(client, ("viele.xml", _fixture(UBL_MANY)))
        doc = IncomingInvoice.query.one()
        rows = doc.parsed
        r = self._book(client, doc, books, date="2015-01-09")
        doc = db.session.get(IncomingInvoice, doc.id)
        if len(rows.vat) > 1:
            assert doc.booking_group_id and f"/booking-groups/{doc.booking_group_id}/edit" in r.headers["Location"]
            assert BookingGroup.query.one().total_amount == -rows.grand_total
        else:
            assert doc.booking_id

    def test_an_existing_supplier_is_used(self, client, admin, pdf_dir, books):
        supplier = Customer(name="Lieferant GmbH", is_supplier=True, is_customer=False)
        db.session.add(supplier)
        db.session.commit()
        _login(client)
        doc = _one(client)
        self._book(client, doc, books, supplier_id=str(supplier.id))
        assert Customer.query.count() == 1 and db.session.get(IncomingInvoice, doc.id).supplier_id == supplier.id

    def test_a_customer_cannot_be_chosen_as_supplier(self, client, admin, pdf_dir, books):
        customer = Customer(name="Nur Kunde", is_supplier=False, is_customer=True)
        db.session.add(customer)
        db.session.commit()
        _login(client)
        doc = _one(client)
        r = self._book(client, doc, books, supplier_id=str(customer.id))
        assert "Lieferanten" in client.get(r.headers["Location"]).get_data(as_text=True)
        assert db.session.get(IncomingInvoice, doc.id).status == "Neu"

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
        r = client.post(f"/accounting/incoming/{doc.id}/book", data=data, follow_redirects=True)
        assert message in r.get_data(as_text=True)
        assert Booking.query.count() == 0 and db.session.get(IncomingInvoice, doc.id).status == "Neu"
        assert Customer.query.count() == 0                        # auch der neue Lieferant ist zurückgerollt

    def test_booking_twice_is_refused(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        self._book(client, doc, books)
        r = client.post(f"/accounting/incoming/{doc.id}/book", data={
            "supplier_id": "new", "date": "2016-04-04", "account_id": str(books.id)}, follow_redirects=True)
        assert "bereits verbucht" in r.get_data(as_text=True) and Booking.query.count() == 1


class TestDiscard:
    def test_discard_keeps_the_original_and_can_be_reopened(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        path = doc.file_path
        r = client.post(f"/accounting/incoming/{doc.id}/discard", follow_redirects=True)
        assert "Die Originaldatei bleibt aufbewahrt" in r.get_data(as_text=True)
        assert db.session.get(IncomingInvoice, doc.id).status == "Verworfen" and os.path.exists(path)
        client.post(f"/accounting/incoming/{doc.id}/reopen")
        assert db.session.get(IncomingInvoice, doc.id).status == "Neu"

    def test_a_booked_document_cannot_be_discarded(self, client, admin, pdf_dir, books):
        _login(client)
        doc = _one(client)
        client.post(f"/accounting/incoming/{doc.id}/book", data={
            "supplier_id": "new", "date": "2016-04-04", "account_id": str(books.id)})
        client.post(f"/accounting/incoming/{doc.id}/discard")
        assert db.session.get(IncomingInvoice, doc.id).status == "Verbucht"


class TestBookingLink:
    """Von der Buchung zurück zum Beleg; Löschen/Storno gibt den Beleg wieder frei."""

    def _booked(self, client, books, fixture=CII, date_="2016-04-04"):
        _upload(client, ("r.xml", _fixture(fixture)))
        doc = IncomingInvoice.query.one()
        client.post(f"/accounting/incoming/{doc.id}/book", data={
            "supplier_id": "new", "date": date_, "account_id": str(books.id)})
        return db.session.get(IncomingInvoice, doc.id)

    def _two_rates(self, client, books):
        """Beleg mit zwei Steuersätzen → Sammelbuchung."""
        import base64  # noqa: F401
        from app.einvoice.incoming import InVat
        _upload(client, ("r.xml", _fixture(CII)))
        doc = IncomingInvoice.query.one()
        parsed = doc.parsed
        parsed.vat = [InVat("S", Decimal("19"), Decimal("200"), Decimal("38")),
                      InVat("S", Decimal("7"), Decimal("100"), Decimal("7"))]
        parsed.grand_total = Decimal("345")
        doc.data = parsed.to_json()
        db.session.commit()
        client.post(f"/accounting/incoming/{doc.id}/book", data={
            "supplier_id": "new", "date": "2016-04-04", "account_id": str(books.id)})
        return db.session.get(IncomingInvoice, doc.id)

    def test_the_booking_page_links_back_to_the_document(self, client, admin, pdf_dir, books):
        _login(client)
        doc = self._booked(client, books)
        html = client.get(f"/accounting/bookings/{doc.booking_id}/edit").get_data(as_text=True)
        assert "Gebucht aus der Eingangsrechnung" in html
        assert f"/accounting/incoming/{doc.id}" in html and "123456XX" in html

    def test_the_group_page_links_back_to_the_document(self, client, admin, pdf_dir, books):
        _login(client)
        doc = self._two_rates(client, books)
        assert doc.booking_group_id
        html = client.get(f"/accounting/booking-groups/{doc.booking_group_id}/edit").get_data(as_text=True)
        assert "Gebucht aus der Eingangsrechnung" in html and f"/accounting/incoming/{doc.id}" in html

    def test_an_ordinary_booking_has_no_link(self, client, admin, pdf_dir, books):
        booking = Booking(date=date(2016, 1, 5), account_id=books.id, amount=Decimal("-10"), description="x")
        db.session.add(booking)
        db.session.commit()
        _login(client)
        assert "Gebucht aus der Eingangsrechnung" not in client.get(
            f"/accounting/bookings/{booking.id}/edit").get_data(as_text=True)

    def test_deleting_the_booking_reopens_the_document(self, client, admin, pdf_dir, books):
        _login(client)
        doc = self._booked(client, books)
        # heute datiert, damit die Buchung löschbar ist (Jahr nicht abgeschlossen, offen)
        r = client.post(f"/accounting/bookings/{doc.booking_id}/delete", follow_redirects=True)
        assert "wieder offen" in r.get_data(as_text=True)
        doc = db.session.get(IncomingInvoice, doc.id)
        assert doc.status == "Neu" and doc.booking_id is None and Booking.query.count() == 0

    def test_deleting_the_group_reopens_the_document(self, client, admin, pdf_dir, books):
        _login(client)
        doc = self._two_rates(client, books)
        client.post(f"/accounting/booking-groups/{doc.booking_group_id}/delete")
        doc = db.session.get(IncomingInvoice, doc.id)
        assert doc.status == "Neu" and doc.booking_group_id is None
        assert BookingGroup.query.count() == 0 and Booking.query.count() == 0

    def test_the_reopened_document_can_be_booked_again(self, client, admin, pdf_dir, books):
        _login(client)
        doc = self._booked(client, books)
        client.post(f"/accounting/bookings/{doc.booking_id}/delete")
        r = client.post(f"/accounting/incoming/{doc.id}/book", data={
            "supplier_id": str(db.session.get(IncomingInvoice, doc.id).supplier_id or "new"),
            "date": "2016-04-04", "account_id": str(books.id)}, follow_redirects=True)
        assert db.session.get(IncomingInvoice, doc.id).status == "Verbucht" and Booking.query.count() == 1
        assert r.status_code == 200


class TestStornoReopens:
    """Storno verlangt Buchungen im laufenden Jahr — die Belege werden mit Datum heute gebucht."""

    def _today_books(self):
        year = date.today().year
        if FiscalYear.query.filter_by(year=year).first() is None:
            db.session.add(FiscalYear(year=year, start_date=date(year, 1, 1), end_date=date(year, 12, 31)))
            db.session.commit()

    def test_storno_of_the_booking_reopens_the_document(self, client, admin, pdf_dir, books):
        self._today_books()
        _login(client)
        _upload(client, ("r.xml", _fixture(CII)))
        doc = IncomingInvoice.query.one()
        client.post(f"/accounting/incoming/{doc.id}/book", data={
            "supplier_id": "new", "date": date.today().isoformat(), "account_id": str(books.id)})
        booking_id = db.session.get(IncomingInvoice, doc.id).booking_id
        client.post(f"/accounting/bookings/{booking_id}/stornieren", data={"storno_reason": "falsch erfasst"})
        assert db.session.get(Booking, booking_id).status == Booking.STATUS_STORNIERT
        doc = db.session.get(IncomingInvoice, doc.id)
        assert doc.status == "Neu" and doc.booking_id is None

    def test_storno_of_the_group_reopens_the_document(self, client, admin, pdf_dir, books):
        from app.einvoice.incoming import InVat
        self._today_books()
        _login(client)
        _upload(client, ("r.xml", _fixture(CII)))
        doc = IncomingInvoice.query.one()
        parsed = doc.parsed
        parsed.vat = [InVat("S", Decimal("19"), Decimal("200"), Decimal("38")),
                      InVat("S", Decimal("7"), Decimal("100"), Decimal("7"))]
        parsed.grand_total = Decimal("345")
        doc.data = parsed.to_json()
        db.session.commit()
        client.post(f"/accounting/incoming/{doc.id}/book", data={
            "supplier_id": "new", "date": date.today().isoformat(), "account_id": str(books.id)})
        group_id = db.session.get(IncomingInvoice, doc.id).booking_group_id
        client.post(f"/accounting/booking-groups/{group_id}/stornieren", data={"storno_reason": "doppelt"})
        assert db.session.get(BookingGroup, group_id).status == BookingGroup.STATUS_STORNIERT
        doc = db.session.get(IncomingInvoice, doc.id)
        assert doc.status == "Neu" and doc.booking_group_id is None
