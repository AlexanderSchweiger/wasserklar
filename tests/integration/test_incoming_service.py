"""Eingangs-E-Rechnungen: Lieferant, Buchungsvorschlag, Doppelte, Verknuepfung mit der Buchung.

Die Ablage der Datei selbst (Upload, Dateityp, Kontingent …) testet ``test_document_service.py``.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.accounting import incoming_service as svc
from app.documents import service as documents_svc
from app.einvoice.incoming import InVat
from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, Document, DocumentLink, FiscalYear, Project, User,
)
from tests.conftest import _ensure_role
from tests.unit.test_einvoice_incoming import _fixture

CII = "kosit-01.01a-INVOICE_uncefact.xml"      # 336,90 € brutto, ein Steuersatz (7 %)
CII_MANY = "kosit-01.13a-INVOICE_uncefact.xml"  # 6342,70 € brutto


@pytest.fixture
def pdf_dir(app, tmp_path, monkeypatch):
    """PDF_DIR als Unterordner — ``documents/`` liegt als Geschwister innerhalb von tmp_path."""
    folder = tmp_path / "pdfs"
    folder.mkdir()
    monkeypatch.setitem(app.config, "PDF_DIR", str(folder))
    return folder


@pytest.fixture
def books(app):
    """Buchungsjahre 2015/2016, ein Ausgabenkonto und ein Anwender."""
    for year in (2015, 2016):
        db.session.add(FiscalYear(year=year, start_date=date(year, 1, 1), end_date=date(year, 12, 31)))
    account = Account(name="Wasserbezug", code="WBZ")
    user = User(username="kassier", email="k@test.test", role_id=_ensure_role("Admin").id)
    user.set_password("secret")
    db.session.add_all([account, user])
    db.session.commit()
    return account, user


def _store(name=CII, upload_name="rechnung.xml", user_id=None):
    return documents_svc.store_upload(upload_name, _fixture(name), user_id)


def _parsed(name=CII):
    return _store(name).einvoice.parsed


class TestPossibleDuplicates:
    def test_by_supplier_and_number(self, app, pdf_dir):
        first = _store()
        second = documents_svc.store_upload("korrigiert.xml", _fixture(CII) + b"<!-- anders -->", None)
        assert svc.possible_duplicates(second) == [first]
        other = _store(CII_MANY, "andere.xml")
        assert svc.possible_duplicates(other) == []

    def test_a_discarded_document_is_no_duplicate(self, app, pdf_dir):
        first = _store()
        second = documents_svc.store_upload("korrigiert.xml", _fixture(CII) + b"<!-- anders -->", None)
        documents_svc.discard(first)
        db.session.commit()
        assert svc.possible_duplicates(second) == []

    def test_a_plain_document_has_none(self, app, pdf_dir):
        plain = documents_svc.store_upload("scan.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, None)
        assert svc.possible_duplicates(plain) == []


class TestSupplier:
    def test_found_by_vat_id_then_by_name(self, app, pdf_dir):
        parsed = _parsed()
        assert svc.suggest_supplier(parsed) is None
        by_name = Customer(name="[SELLER NAME]", is_supplier=True, is_customer=False)
        db.session.add(by_name)
        db.session.commit()
        assert svc.suggest_supplier(parsed) == by_name            # Name, Groß-/Kleinschreibung egal
        by_vat = Customer(name="Ganz anders", is_supplier=True, is_customer=False, vat_id="DE123456789")
        db.session.add(by_vat)
        db.session.commit()
        assert svc.suggest_supplier(parsed) == by_vat            # die USt-IdNr. gewinnt

    def test_customers_and_archived_contacts_are_not_suggested(self, app, pdf_dir):
        parsed = _parsed()
        db.session.add_all([
            Customer(name="[Seller name]", is_supplier=False, is_customer=True, vat_id="DE123456789"),
            Customer(name="[Seller name]", is_supplier=True, is_customer=False, active=False),
        ])
        db.session.commit()
        assert svc.suggest_supplier(parsed) is None

    def test_create_supplier_from_the_invoice(self, app, pdf_dir):
        supplier = svc.create_supplier(_parsed())
        assert supplier.id and supplier.is_supplier and not supplier.is_customer
        assert supplier.vat_id == "DE123456789" and supplier.is_company
        assert supplier.customer_number is None               # Lieferanten ziehen keine Kundennummer
        assert supplier.land == "Deutschland"


class TestBookingRows:
    def test_single_rate(self, app, pdf_dir):
        rows = svc.booking_rows(_parsed())
        assert [(r["rate"], r["gross"]) for r in rows] == [(Decimal("7"), Decimal("-336.90"))]

    def test_credit_note_books_positive(self, app, pdf_dir):
        parsed = _parsed()
        parsed.type_code = "381"
        assert svc.booking_rows(parsed)[0]["gross"] == Decimal("336.90")

    def test_rates_are_merged_and_zero_rate_has_no_tax_label(self, app, pdf_dir):
        parsed = _parsed()
        parsed.vat = [InVat("S", Decimal("7"), Decimal("100"), Decimal("7")),
                      InVat("E", Decimal("0"), Decimal("20"), Decimal("0")),
                      InVat("Z", Decimal("0"), Decimal("10"), Decimal("0"))]
        parsed.grand_total = Decimal("137")
        rows = svc.booking_rows(parsed)
        assert [(r["rate"], r["gross"], r["label"]) for r in rows] == [
            (Decimal("7"), Decimal("-107"), "7 % USt"), (Decimal("0"), Decimal("-30"), "ohne USt")]

    def test_a_cent_of_rounding_goes_into_the_first_row(self, app, pdf_dir):
        parsed = _parsed()
        parsed.grand_total = Decimal("336.91")
        assert sum(r["gross"] for r in svc.booking_rows(parsed)) == Decimal("-336.91")

    def test_a_real_discrepancy_is_an_error(self, app, pdf_dir):
        parsed = _parsed()
        parsed.grand_total = Decimal("400.00")
        with pytest.raises(svc.BookingError, match="Steuerbeträge der Datei"):
            svc.booking_rows(parsed)

    def test_without_vat_data_the_total_is_booked_untaxed(self, app, pdf_dir):
        parsed = _parsed()
        parsed.vat = []
        rows = svc.booking_rows(parsed)
        assert [(r["rate"], r["gross"]) for r in rows] == [(Decimal("0"), Decimal("-336.90"))]

    def test_the_default_date_is_never_in_the_future(self, app, pdf_dir):
        parsed = _parsed()
        assert svc.default_booking_date(parsed) == date(2016, 4, 4)
        parsed.issue_date = date(2999, 1, 1)
        assert svc.default_booking_date(parsed) == date.today()


class TestBook:
    def _supplier(self):
        supplier = Customer(name="Lieferant GmbH", is_supplier=True, is_customer=False, vat_id="DE123456789")
        db.session.add(supplier)
        db.session.commit()
        return supplier

    def test_a_single_rate_becomes_one_booking(self, app, pdf_dir, books):
        account, user = books
        supplier = self._supplier()
        doc = _store()
        booking = svc.book(doc, supplier=supplier, account_id=account.id,
                           booking_date=date(2016, 4, 4), user_id=user.id)
        db.session.commit()
        assert isinstance(booking, Booking)
        assert (booking.amount, booking.tax_rate, booking.account_id) == (Decimal("-336.90"), Decimal("7.00"), account.id)
        assert booking.customer_id == supplier.id and booking.reference == "123456XX"
        assert "Lieferant GmbH: Rechnung 123456XX" in booking.description and booking.group_id is None
        assert documents_svc.is_booked(doc) and doc.supplier_id == supplier.id
        link = DocumentLink.query.one()
        assert (link.document_id, link.booking_id, link.booking_group_id) == (doc.id, booking.id, None)

    def test_several_rates_become_a_booking_group(self, app, pdf_dir, books):
        account, user = books
        supplier = self._supplier()
        project = Project(name="Sanierung")
        db.session.add(project)
        db.session.commit()
        doc = _store()
        parsed = doc.einvoice.parsed
        parsed.vat = [InVat("S", Decimal("19"), Decimal("200"), Decimal("38")),
                      InVat("S", Decimal("7"), Decimal("100"), Decimal("7"))]
        parsed.grand_total = Decimal("345")
        doc.einvoice.data = parsed.to_json()
        group = svc.book(doc, supplier=supplier, account_id=account.id, booking_date=date(2016, 4, 4),
                         user_id=user.id, project_id=project.id)
        db.session.commit()
        assert isinstance(group, BookingGroup) and group.total_amount == Decimal("-345.00")
        children = Booking.query.filter_by(group_id=group.id).order_by(Booking.id).all()
        assert [(c.amount, c.tax_rate, c.project_id) for c in children] == [
            (Decimal("-238.00"), Decimal("19.00"), project.id), (Decimal("-107.00"), Decimal("7.00"), project.id)]
        assert "(19 % USt)" in children[0].description
        assert DocumentLink.query.one().booking_group_id == group.id        # am Header, nicht an einem Kind

    def test_a_credit_note_is_booked_as_income(self, app, pdf_dir, books):
        account, user = books
        supplier = self._supplier()
        doc = _store()
        parsed = doc.einvoice.parsed
        parsed.type_code = "381"
        doc.einvoice.data = parsed.to_json()
        doc.einvoice.type_code = "381"
        booking = svc.book(doc, supplier=supplier, account_id=account.id,
                           booking_date=date(2016, 4, 4), user_id=user.id)
        assert booking.amount == Decimal("336.90") and "Gutschrift 123456XX" in booking.description

    @pytest.mark.parametrize("kwargs, message", [
        ({"supplier": None}, "Lieferanten"),
        ({"account_id": None}, "Konto"),
        ({"booking_date": date(2999, 1, 1)}, "Zukunft"),
        ({"booking_date": date(2010, 1, 1)}, "Buchungsjahr"),
        ({"project_id": 99999}, "Projekt"),
        ({"real_account_id": 99999}, "Bankkonto"),
    ])
    def test_refusals_leave_the_document_open(self, app, pdf_dir, books, kwargs, message):
        account, user = books
        params = dict(supplier=self._supplier(), account_id=account.id,
                      booking_date=date(2016, 4, 4), user_id=user.id)
        params.update(kwargs)
        doc = _store()
        with pytest.raises(svc.BookingError, match=message):
            svc.book(doc, **params)
        assert not documents_svc.is_booked(doc) and Booking.query.count() == 0 and DocumentLink.query.count() == 0

    def test_a_closed_fiscal_year_is_refused(self, app, pdf_dir, books):
        account, user = books
        FiscalYear.query.filter_by(year=2016).one().closed = True
        db.session.commit()
        doc = _store()
        with pytest.raises(svc.BookingError, match="abgeschlossen"):
            svc.book(doc, supplier=self._supplier(), account_id=account.id,
                     booking_date=date(2016, 4, 4), user_id=user.id)

    def test_booking_twice_is_refused(self, app, pdf_dir, books):
        account, user = books
        supplier = self._supplier()
        doc = _store()
        svc.book(doc, supplier=supplier, account_id=account.id, booking_date=date(2016, 4, 4), user_id=user.id)
        db.session.commit()
        with pytest.raises(svc.BookingError, match="bereits verbucht"):
            svc.book(doc, supplier=supplier, account_id=account.id,
                     booking_date=date(2016, 4, 4), user_id=user.id)
        assert Booking.query.count() == 1

    def test_a_discarded_document_cannot_be_booked(self, app, pdf_dir, books):
        account, user = books
        doc = _store()
        documents_svc.discard(doc)
        with pytest.raises(svc.BookingError, match="verworfen"):
            svc.book(doc, supplier=self._supplier(), account_id=account.id,
                     booking_date=date(2016, 4, 4), user_id=user.id)

    def test_a_filed_document_can_still_be_booked(self, app, pdf_dir, books):
        account, user = books
        doc = _store()
        documents_svc.shelve(doc)
        svc.book(doc, supplier=self._supplier(), account_id=account.id,
                 booking_date=date(2016, 4, 4), user_id=user.id)
        assert doc.status == "Neu" and documents_svc.is_booked(doc)

    def test_a_document_without_einvoice_data_cannot_be_suggested(self, app, pdf_dir, books):
        account, user = books
        plain = documents_svc.store_upload("scan.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, None)
        with pytest.raises(svc.BookingError, match="keine E-Rechnungsdaten"):
            svc.book(plain, supplier=self._supplier(), account_id=account.id,
                     booking_date=date(2016, 4, 4), user_id=user.id)

    def test_after_a_storno_the_document_can_be_booked_again(self, app, pdf_dir, books):
        account, user = books
        supplier = self._supplier()
        doc = _store()
        first = svc.book(doc, supplier=supplier, account_id=account.id, booking_date=date(2016, 4, 4), user_id=user.id)
        db.session.commit()
        first.status = Booking.STATUS_STORNIERT
        db.session.commit()
        second = svc.book(doc, supplier=supplier, account_id=account.id, booking_date=date(2016, 4, 5), user_id=user.id)
        db.session.commit()
        assert second.id != first.id and documents_svc.is_booked(doc)
        assert Document.query.count() == 1 and DocumentLink.query.count() == 2     # Nachweis + neue Buchung
