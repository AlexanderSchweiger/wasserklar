"""Steuersatz nach dem Verbuchen + Steuersatz im Bankimport.

* Eine verbuchte Buchung ohne Rechnung darf ihren Steuersatz noch ändern (Brutto bleibt gleich),
  jede Änderung steht im ``BookingChange``-Protokoll. Datum/Betrag/Belegnummer bleiben gesperrt.
* Gesperrt bleibt er bei Rechnungs-Buchungen, Storno-Paaren, abgeschlossenen Jahren und
  übergebenen Buchungen; die Sammeländerung überspringt diese Buchungen.
* Der Bankimport bucht Zeilen ohne Rechnung mit dem gewählten Satz, sonst mit dem
  Standard-Steuersatz des Kontos.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.accounting import services as acc_svc
from app.bank_import import services as bank_services
from app.extensions import db
from app.models import (AccountingHandover, AccountingHandoverItem, Account, BankStatement,
                        BankStatementLine, Booking, BookingChange, Customer, FiscalYear, Invoice,
                        OpenItem, RealAccount, User)
from tests.conftest import _ensure_role

HX = {"HX-Request": "true"}
YEAR = date.today().year


@pytest.fixture
def setup(client, app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    fy = FiscalYear(year=YEAR, start_date=date(YEAR, 1, 1), end_date=date(YEAR, 12, 31),
                    closed=False, is_vat_liable=True)
    acc = Account(name="Strom", default_tax_rate=Decimal("20"))
    acc_plain = Account(name="Spenden")
    ra = RealAccount(name="Giro", opening_balance=Decimal("0"))
    db.session.add_all([u, fy, acc, acc_plain, ra])
    db.session.flush()
    b = Booking(date=date(YEAR, 1, 2), account_id=acc.id, amount=Decimal("-120"),
                description="Strom Jänner", reference="BA-1", status="Verbucht", real_account_id=ra.id)
    db.session.add(b)
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return dict(u=u, b=b, acc=acc, acc_plain=acc_plain, ra=ra, fy=fy)


def _payload(s, **over):
    data = {"account_id": s["acc"].id, "description": "Strom Jänner", "project_id": "",
            "customer_id": "", "real_account_id": s["ra"].id, "action": "",
            "date": f"{YEAR}-01-02", "amount": "-120", "reference": "BA-1", "tax_rate": ""}
    data.update(over)
    return data


class TestPostedBookingTax:
    def test_form_offers_the_tax_rate(self, client, setup):
        html = client.get(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX).get_data(as_text=True)
        assert "Der Steuersatz lässt sich noch korrigieren" in html
        assert 'name="tax_rate" id="tax_rate"' in html
        assert '<input type="hidden" name="tax_rate"' not in html

    def test_change_is_saved_and_logged(self, client, setup):
        r = client.post(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX,
                        data=_payload(setup, tax_rate="20", amount="-999", date=f"{YEAR}-01-05",
                                      reference="X"))
        assert "booking-saved" in r.headers.get("HX-Trigger", "")
        b = db.session.get(Booking, setup["b"].id)
        assert b.tax_rate == Decimal("20")
        # gesperrte Felder bleiben, wie sie waren
        assert b.amount == Decimal("-120") and b.date == date(YEAR, 1, 2) and b.reference == "BA-1"
        change = BookingChange.query.one()
        assert (change.field, change.old_value, change.new_value) == ("tax_rate", None, "20")
        assert change.changed_by_id == setup["u"].id
        html = client.get(f"/accounting/bookings/{b.id}/edit", headers=HX).get_data(as_text=True)
        assert "Änderungen nach dem Verbuchen" in html

    def test_unchanged_save_logs_nothing(self, client, setup):
        client.post(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX, data=_payload(setup))
        assert BookingChange.query.count() == 0

    def test_open_booking_changes_without_log(self, client, setup):
        setup["b"].status = Booking.STATUS_OFFEN
        db.session.commit()
        client.post(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX,
                    data=_payload(setup, tax_rate="20"))
        assert db.session.get(Booking, setup["b"].id).tax_rate == Decimal("20")
        assert BookingChange.query.count() == 0

    def test_invoice_booking_stays_locked(self, client, setup):
        cust = Customer(name="Huber")
        db.session.add(cust)
        db.session.flush()
        inv = Invoice(invoice_number=f"{YEAR}-00001", customer_id=cust.id, date=date(YEAR, 1, 1),
                      status="Bezahlt", total_amount=Decimal("120"))
        db.session.add(inv)
        db.session.flush()
        setup["b"].invoice_id = inv.id
        db.session.commit()
        html = client.get(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX).get_data(as_text=True)
        assert "Der Steuersatz kommt aus der Rechnung" in html
        r = client.post(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX,
                        data=_payload(setup, tax_rate="20"))
        assert "booking-saved" not in r.headers.get("HX-Trigger", "")
        assert db.session.get(Booking, setup["b"].id).tax_rate is None

    def test_handed_over_booking_stays_locked(self, client, setup):
        h = AccountingHandover(format="datev", fiscal_year=YEAR, date_from=date(YEAR, 1, 1),
                               date_to=date(YEAR, 1, 31), created_at=datetime(YEAR, 2, 1))
        db.session.add(h)
        db.session.flush()
        db.session.add(AccountingHandoverItem(handover_id=h.id, booking_id=setup["b"].id))
        db.session.commit()
        assert acc_svc.tax_change_blocker(setup["b"])
        client.post(f"/accounting/bookings/{setup['b'].id}/edit", headers=HX,
                    data=_payload(setup, tax_rate="20"))
        assert db.session.get(Booking, setup["b"].id).tax_rate is None

    def test_closed_year_and_storno_block(self, setup):
        b = setup["b"]
        b.storno_of_id = b.id            # nur Kennzeichen: Gegenbuchung
        assert "Storno" in acc_svc.tax_change_blocker(b)
        b.storno_of_id = None
        setup["fy"].closed = True
        assert "abgeschlossen" in acc_svc.tax_change_blocker(b)


class TestBulkTax:
    def test_bulk_sets_tax_on_posted_bookings_and_skips_invoices(self, client, setup):
        cust = Customer(name="Huber")
        db.session.add(cust)
        db.session.flush()
        inv = Invoice(invoice_number=f"{YEAR}-00002", customer_id=cust.id, date=date(YEAR, 1, 1),
                      status="Bezahlt", total_amount=Decimal("50"))
        db.session.add(inv)
        db.session.flush()
        b2 = Booking(date=date(YEAR, 1, 3), account_id=setup["acc"].id, amount=Decimal("50"),
                     description="Zahlung", status="Verbucht", invoice_id=inv.id,
                     tax_rate=Decimal("10"))
        db.session.add(b2)
        db.session.commit()
        r = client.post("/accounting/bookings/bulk-edit", data={
            "booking_ids": [setup["b"].id, b2.id], "bulk_tax_rate": "20", "year": YEAR})
        html = r.get_data(as_text=True)
        assert "1 Buchung(en) aktualisiert" in html
        assert "wurde der Steuersatz nicht geändert" in html
        assert db.session.get(Booking, setup["b"].id).tax_rate == Decimal("20")
        assert db.session.get(Booking, b2.id).tax_rate == Decimal("10")
        assert BookingChange.query.count() == 1

    def test_bulk_can_remove_tax(self, client, setup):
        setup["b"].tax_rate = Decimal("20")
        db.session.commit()
        client.post("/accounting/bookings/bulk-edit", data={
            "booking_ids": [setup["b"].id], "bulk_tax_rate": "0", "year": YEAR})
        assert db.session.get(Booking, setup["b"].id).tax_rate is None
        assert BookingChange.query.one().new_value is None

    def test_empty_bulk_tax_changes_nothing(self, client, setup):
        client.post("/accounting/bookings/bulk-edit", data={
            "booking_ids": [setup["b"].id], "bulk_tax_rate": "", "year": YEAR})
        assert db.session.get(Booking, setup["b"].id).tax_rate is None


class TestAccountDefault:
    def test_modal_saves_default_tax_only_with_marker(self, client, setup):
        acc = setup["acc_plain"]
        client.post(f"/accounting/accounts/{acc.id}/edit", headers={"X-From-Modal": "1"},
                    data={"name": "Spenden", "code": "", "active": "1",
                          "account_tax_field": "1", "default_tax_rate": "10"})
        assert db.session.get(Account, acc.id).default_tax_rate == Decimal("10")
        # Formular ohne das Feld (Vollseite) ändert nichts
        client.post(f"/accounting/accounts/{acc.id}/edit",
                    data={"name": "Spenden", "code": "", "active": "1"})
        assert db.session.get(Account, acc.id).default_tax_rate == Decimal("10")

    def test_booking_form_carries_the_default(self, client, setup):
        html = client.get("/accounting/bookings/new", headers=HX).get_data(as_text=True)
        assert 'data-default-tax="20' in html


def _statement(setup, amount="-60"):
    stmt = BankStatement(format="camt053", filename="a.xml", file_hash="x" * 64,
                         real_account_id=setup["ra"].id)
    db.session.add(stmt)
    db.session.flush()
    line = BankStatementLine(statement_id=stmt.id, line_index=1, booking_date=date(YEAR, 1, 10),
                             amount=Decimal(amount), purpose="Strom", selected=True)
    db.session.add(line)
    db.session.commit()
    return stmt, line


class TestBankImportTax:
    def test_account_default_is_used(self, setup):
        stmt, line = _statement(setup)
        line.override_account_id = setup["acc"].id
        db.session.commit()
        stats = bank_services.commit_statement(stmt.id, setup["u"].id)
        assert stats["errors"] == []
        assert db.session.get(Booking, line.booking_id).tax_rate == Decimal("20")

    def test_chosen_rate_beats_the_default(self, client, setup):
        stmt, line = _statement(setup)
        url = f"/bank-import/statements/{stmt.id}/lines/{line.id}"
        client.post(url, data={"action": "set_account", "account_id": setup["acc"].id})
        r = client.post(url, data={"action": "set_tax", "tax_rate": "10"}, headers=HX)
        assert r.status_code == 200
        assert db.session.get(BankStatementLine, line.id).override_tax_rate == Decimal("10")
        bank_services.commit_statement(stmt.id, setup["u"].id)
        line = db.session.get(BankStatementLine, line.id)
        assert db.session.get(Booking, line.booking_id).tax_rate == Decimal("10")

    def test_explicit_zero_means_no_tax(self, client, setup):
        stmt, line = _statement(setup)
        line.override_account_id = setup["acc"].id
        line.override_tax_rate = Decimal("0")
        db.session.commit()
        bank_services.commit_statement(stmt.id, setup["u"].id)
        assert db.session.get(Booking, line.booking_id).tax_rate is None

    def test_row_warns_without_tax_in_vat_year(self, client, setup):
        stmt, line = _statement(setup)
        url = f"/bank-import/statements/{stmt.id}/lines/{line.id}"
        html = client.post(url, data={"action": "set_account", "account_id": setup["acc_plain"].id},
                           headers=HX).get_data(as_text=True)
        assert 'name="tax_rate"' in html
        assert "Ohne Steuer — bitte prüfen" in html

    def test_manual_open_item_uses_line_tax(self, setup):
        cust = Customer(name="Mieter")
        db.session.add(cust)
        db.session.flush()
        op = OpenItem(customer_id=cust.id, description="Miete Lager", amount=Decimal("60"),
                      date=date(YEAR, 1, 1), account_id=setup["acc"].id)
        db.session.add(op)
        db.session.flush()
        stmt, line = _statement(setup, amount="60")
        line.matched_open_item_id = op.id
        db.session.commit()
        bank_services.commit_statement(stmt.id, setup["u"].id)
        line = db.session.get(BankStatementLine, line.id)
        assert db.session.get(Booking, line.booking_id).tax_rate == Decimal("20")
