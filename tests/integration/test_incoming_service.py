"""Eingangs-E-Rechnungen: Ablage, Lieferant, Buchungsvorschlag, Export/Import."""
import hashlib
import io
import os
from datetime import date
from decimal import Decimal

import pytest

from app.accounting import incoming_service as svc
from app.data_transfer.services import export_to_zip, extract_to_temp, import_from_zip
from app.einvoice.incoming import IncomingError, parse
from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, FiscalYear, IncomingInvoice, Project, User,
)
from tests.conftest import _ensure_role
from tests.unit.test_einvoice_incoming import _fixture, _pdf_with

CII = "kosit-01.01a-INVOICE_uncefact.xml"      # 336,90 € brutto, ein Steuersatz (7 %)
CII_MANY = "kosit-01.13a-INVOICE_uncefact.xml"  # 6342,70 € brutto


@pytest.fixture
def pdf_dir(app, tmp_path, monkeypatch):
    """PDF_DIR als Unterordner — ``incoming/`` liegt als Geschwister innerhalb von tmp_path."""
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
    doc = svc.store_upload(upload_name, _fixture(name), user_id)
    db.session.commit()
    return doc


class TestStoreUpload:
    def test_the_original_is_kept_unchanged(self, app, pdf_dir):
        data = _fixture(CII)
        with app.test_request_context():
            doc = _store()
        assert doc.file_path.startswith(str(pdf_dir.parent / "incoming"))
        with open(doc.file_path, "rb") as fh:
            assert fh.read() == data                      # Byte für Byte
        assert doc.sha256 == hashlib.sha256(data).hexdigest()
        assert os.path.basename(doc.file_path).endswith("_rechnung.xml")

    def test_the_columns_and_json_are_filled(self, app, pdf_dir):
        with app.test_request_context():
            doc = _store()
        assert (doc.number, doc.syntax, doc.source_kind, doc.currency, doc.type_code) == \
            ("123456XX", "cii", "xml", "EUR", "380")
        assert doc.issue_date == date(2016, 4, 4) and doc.grand_total == Decimal("336.90")
        assert doc.seller_vat_id == "DE123456789" and doc.status == IncomingInvoice.STATUS_NEW
        assert doc.parsed.number == "123456XX" and doc.format_label == "XRechnung (XML)"
        assert doc.issue_date.year == 2016 and f"{os.sep}2016{os.sep}" in doc.file_path

    def test_a_pdf_keeps_the_pdf_and_reads_the_xml(self, app, pdf_dir):
        pdf = _pdf_with(_fixture(CII))
        with app.test_request_context():
            doc = svc.store_upload("lieferant.pdf", pdf, None)
            db.session.commit()
        assert doc.source_kind == "pdf" and doc.format_label == "ZUGFeRD / Factur-X (PDF)"
        with open(doc.file_path, "rb") as fh:
            assert fh.read() == pdf

    def test_the_same_file_twice_is_refused(self, app, pdf_dir):
        with app.test_request_context():
            first = _store()
            with pytest.raises(svc.DuplicateUpload) as exc:
                svc.store_upload("nochmal.xml", _fixture(CII), None)
        assert exc.value.existing.id == first.id
        assert IncomingInvoice.query.count() == 1

    def test_an_unreadable_file_leaves_nothing_behind(self, app, pdf_dir):
        with app.test_request_context():
            with pytest.raises(IncomingError):
                svc.store_upload("x.pdf", _pdf_with(None), None)
        assert IncomingInvoice.query.count() == 0
        assert not (pdf_dir.parent / "incoming").exists()

    def test_the_size_limit(self, app, pdf_dir):
        with app.test_request_context(), pytest.raises(IncomingError, match="15 MB"):
            svc.store_upload("gross.xml", b"x" * (svc.MAX_UPLOAD_BYTES + 1), None)

    def test_a_dangerous_file_name_is_neutralised(self, app, pdf_dir):
        with app.test_request_context():
            doc = svc.store_upload("../../etc/pass wd.xml", _fixture(CII), None)
        assert os.path.dirname(doc.file_path).startswith(str(pdf_dir.parent / "incoming"))
        assert ".." not in os.path.basename(doc.file_path)

    def test_possible_duplicates_by_supplier_and_number(self, app, pdf_dir):
        with app.test_request_context():
            first = _store()
            second = svc.store_upload("korrigiert.xml", _fixture(CII) + b"<!-- anders -->", None)
            db.session.commit()
            assert svc.possible_duplicates(second) == [first]
            other = _store(CII_MANY, "andere.xml")
            assert svc.possible_duplicates(other) == []


class TestSupplier:
    def test_found_by_vat_id_then_by_name(self, app, pdf_dir):
        with app.test_request_context():
            parsed = _store().parsed
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
        with app.test_request_context():
            parsed = _store().parsed
        db.session.add_all([
            Customer(name="[Seller name]", is_supplier=False, is_customer=True, vat_id="DE123456789"),
            Customer(name="[Seller name]", is_supplier=True, is_customer=False, active=False),
        ])
        db.session.commit()
        assert svc.suggest_supplier(parsed) is None

    def test_create_supplier_from_the_invoice(self, app, pdf_dir):
        with app.test_request_context():
            parsed = _store().parsed
            supplier = svc.create_supplier(parsed)
        assert supplier.id and supplier.is_supplier and not supplier.is_customer
        assert supplier.vat_id == "DE123456789" and supplier.is_company
        assert supplier.customer_number is None               # Lieferanten ziehen keine Kundennummer
        assert supplier.land == "Deutschland"


class TestBookingRows:
    def test_single_rate(self, app, pdf_dir):
        with app.test_request_context():
            rows = svc.booking_rows(_store().parsed)
        assert [(r["rate"], r["gross"]) for r in rows] == [(Decimal("7"), Decimal("-336.90"))]

    def test_credit_note_books_positive(self, app, pdf_dir):
        with app.test_request_context():
            parsed = _store().parsed
        parsed.type_code = "381"
        assert svc.booking_rows(parsed)[0]["gross"] == Decimal("336.90")

    def test_rates_are_merged_and_zero_rate_has_no_tax_label(self, app, pdf_dir):
        with app.test_request_context():
            parsed = _store().parsed
        from app.einvoice.incoming import InVat
        parsed.vat = [InVat("S", Decimal("7"), Decimal("100"), Decimal("7")),
                      InVat("E", Decimal("0"), Decimal("20"), Decimal("0")),
                      InVat("Z", Decimal("0"), Decimal("10"), Decimal("0"))]
        parsed.grand_total = Decimal("137")
        rows = svc.booking_rows(parsed)
        assert [(r["rate"], r["gross"], r["label"]) for r in rows] == [
            (Decimal("7"), Decimal("-107"), "7 % USt"), (Decimal("0"), Decimal("-30"), "ohne USt")]

    def test_a_cent_of_rounding_goes_into_the_first_row(self, app, pdf_dir):
        with app.test_request_context():
            parsed = _store().parsed
        parsed.grand_total = Decimal("336.91")
        assert sum(r["gross"] for r in svc.booking_rows(parsed)) == Decimal("-336.91")

    def test_a_real_discrepancy_is_an_error(self, app, pdf_dir):
        with app.test_request_context():
            parsed = _store().parsed
        parsed.grand_total = Decimal("400.00")
        with pytest.raises(svc.BookingError, match="Steuerbeträge der Datei"):
            svc.booking_rows(parsed)

    def test_without_vat_data_the_total_is_booked_untaxed(self, app, pdf_dir):
        with app.test_request_context():
            parsed = _store().parsed
        parsed.vat = []
        rows = svc.booking_rows(parsed)
        assert [(r["rate"], r["gross"]) for r in rows] == [(Decimal("0"), Decimal("-336.90"))]

    def test_the_default_date_is_never_in_the_future(self, app, pdf_dir):
        with app.test_request_context():
            parsed = _store().parsed
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
        with app.test_request_context():
            doc = _store()
            booking = svc.book(doc, supplier=supplier, account_id=account.id,
                               booking_date=date(2016, 4, 4), user_id=user.id)
            db.session.commit()
        assert isinstance(booking, Booking)
        assert (booking.amount, booking.tax_rate, booking.account_id) == (Decimal("-336.90"), Decimal("7.00"), account.id)
        assert booking.customer_id == supplier.id and booking.reference == "123456XX"
        assert "Lieferant GmbH: Rechnung 123456XX" in booking.description and booking.group_id is None
        assert doc.status == IncomingInvoice.STATUS_BOOKED
        assert (doc.supplier_id, doc.booking_id, doc.booking_group_id) == (supplier.id, booking.id, None)

    def test_several_rates_become_a_booking_group(self, app, pdf_dir, books):
        account, user = books
        supplier = self._supplier()
        project = Project(name="Sanierung")
        db.session.add(project)
        db.session.commit()
        with app.test_request_context():
            doc = _store()
            parsed = doc.parsed
            from app.einvoice.incoming import InVat
            parsed.vat = [InVat("S", Decimal("19"), Decimal("200"), Decimal("38")),
                          InVat("S", Decimal("7"), Decimal("100"), Decimal("7"))]
            parsed.grand_total = Decimal("345")
            doc.data = parsed.to_json()
            group = svc.book(doc, supplier=supplier, account_id=account.id, booking_date=date(2016, 4, 4),
                             user_id=user.id, project_id=project.id)
            db.session.commit()
        assert isinstance(group, BookingGroup) and group.total_amount == Decimal("-345.00")
        children = Booking.query.filter_by(group_id=group.id).order_by(Booking.id).all()
        assert [(c.amount, c.tax_rate, c.project_id) for c in children] == [
            (Decimal("-238.00"), Decimal("19.00"), project.id), (Decimal("-107.00"), Decimal("7.00"), project.id)]
        assert "(19 % USt)" in children[0].description and doc.booking_group_id == group.id

    def test_a_credit_note_is_booked_as_income(self, app, pdf_dir, books):
        account, user = books
        supplier = self._supplier()
        with app.test_request_context():
            doc = _store()
            parsed = doc.parsed
            parsed.type_code = "381"
            doc.data = parsed.to_json()
            doc.type_code = "381"
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
        with app.test_request_context():
            doc = _store()
            with pytest.raises(svc.BookingError, match=message):
                svc.book(doc, **params)
        assert doc.status == IncomingInvoice.STATUS_NEW and Booking.query.count() == 0

    def test_a_closed_fiscal_year_is_refused(self, app, pdf_dir, books):
        account, user = books
        FiscalYear.query.filter_by(year=2016).one().closed = True
        db.session.commit()
        with app.test_request_context():
            doc = _store()
            with pytest.raises(svc.BookingError, match="abgeschlossen"):
                svc.book(doc, supplier=self._supplier(), account_id=account.id,
                         booking_date=date(2016, 4, 4), user_id=user.id)

    def test_booking_twice_is_refused(self, app, pdf_dir, books):
        account, user = books
        supplier = self._supplier()
        with app.test_request_context():
            doc = _store()
            svc.book(doc, supplier=supplier, account_id=account.id, booking_date=date(2016, 4, 4), user_id=user.id)
            db.session.commit()
            with pytest.raises(svc.BookingError, match="bereits verbucht"):
                svc.book(doc, supplier=supplier, account_id=account.id,
                         booking_date=date(2016, 4, 4), user_id=user.id)
        assert Booking.query.count() == 1


class TestExportImport:
    def test_documents_and_originals_survive_a_full_roundtrip(self, app, pdf_dir, books, tmp_path):
        account, user = books
        supplier = Customer(name="Lieferant GmbH", is_supplier=True, is_customer=False, vat_id="DE123456789")
        db.session.add(supplier)
        db.session.commit()
        with app.test_request_context():
            doc = _store(upload_name="Ä Rechnung.xml")
            svc.book(doc, supplier=supplier, account_id=account.id, booking_date=date(2016, 4, 4), user_id=user.id)
            db.session.commit()
            original = open(doc.file_path, "rb").read()
            sha = doc.sha256

            selection = {"stammdaten": True, "buchungen": True, "mahnwesen": True, "einstellungen": True,
                         "include_pdfs": True, "years": []}
            buf = io.BytesIO()
            export_to_zip(selection, buf, exported_by="test")
            buf.seek(0)
            os.remove(doc.file_path)                          # Original ist weg — das Bundle bringt es zurück
            extract_dir, manifest = extract_to_temp(buf, str(tmp_path))
            import_from_zip(extract_dir, manifest, mode="replace", instance_path=str(tmp_path))
            db.session.remove()

            again = IncomingInvoice.query.filter_by(sha256=sha).one()
            assert again.status == IncomingInvoice.STATUS_BOOKED and again.number == "123456XX"
            assert again.parsed.grand_total == Decimal("336.90")
            assert again.booking_id and again.supplier_id                      # Verknüpfungen bleiben
            assert os.path.exists(again.file_path)
            assert again.file_path.startswith(str(pdf_dir.parent / "incoming"))
            assert open(again.file_path, "rb").read() == original

    def test_without_pdfs_the_rows_stay_but_the_path_is_empty(self, app, pdf_dir, books, tmp_path):
        with app.test_request_context():
            doc = _store()
            sha = doc.sha256
            selection = {"stammdaten": True, "buchungen": True, "mahnwesen": True, "einstellungen": True,
                         "include_pdfs": False, "years": []}
            buf = io.BytesIO()
            export_to_zip(selection, buf, exported_by="test")
            buf.seek(0)
            extract_dir, manifest = extract_to_temp(buf, str(tmp_path))
            import_from_zip(extract_dir, manifest, mode="replace", instance_path=str(tmp_path))
            db.session.remove()
            again = IncomingInvoice.query.filter_by(sha256=sha).one()
        assert again.file_path is None and again.number == "123456XX"
