"""HTTP-Tests fuer die Kontierung beim Bezahlt-Setzen (v1.43.0).

Regression: „Bezahlt" auf der Rechnung rief
``booking_group_from_invoice_payment`` ohne Fallback-Konto auf. Weil kein
Rechnungs-Template je ein ``account_id`` sendete, war ``OpenItem.account_id``
immer NULL — der Service warf ``ValueError`` und der Statuswechsel wurde
komplett zurueckgerollt. Jetzt gilt: kontierte Positionen buchen direkt, und
fehlt eine Kontierung, fragt der Dialog das Konto ab.

Ausserdem: die Sammelaktion „Bezahlt" aus der Rechnungsliste hat den Status
gesetzt, aber gar nichts gebucht (Asymmetrie zum Einzel-Button).
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, Invoice, InvoiceItem,
    OpenItem, RealAccount, User,
)
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post(
        "/auth/login", data={"username": "admin", "password": "secret"})


@pytest.fixture
def accounts(app):
    a1 = Account(name="Wassererlöse", code="W01")
    a2 = Account(name="Grundgebühren", code="G01")
    ra = RealAccount(name="Girokonto", iban="AT99", opening_balance=Decimal("0"),
                     is_default=True)
    db.session.add_all([a1, a2, ra])
    db.session.commit()
    return a1, a2, ra


def _sent_invoice(number, items):
    """Versendete Rechnung inkl. Offenem Posten (ohne Konto) anlegen."""
    cust = Customer.query.filter_by(name="Kunde").first()
    if cust is None:
        cust = Customer(name="Kunde", customer_number=1)
        db.session.add(cust)
        db.session.flush()
    total = sum(Decimal(str(i["amount"])) for i in items)
    inv = Invoice(
        invoice_number=number,
        customer_id=cust.id,
        date=date(2024, 6, 1),
        due_date=date(2024, 7, 1),
        status=Invoice.STATUS_SENT,
        total_amount=total,
    )
    db.session.add(inv)
    db.session.flush()
    for i in items:
        db.session.add(InvoiceItem(
            invoice_id=inv.id, description=i.get("desc", "Pos"),
            quantity=Decimal("1"), unit="Stk",
            unit_price=Decimal(str(i["amount"])), amount=Decimal(str(i["amount"])),
            account_id=i.get("account_id")))
    db.session.add(OpenItem(
        customer_id=cust.id, description=number, amount=total,
        date=inv.date, due_date=inv.due_date, status=OpenItem.STATUS_OPEN,
        invoice_id=inv.id))
    db.session.commit()
    return inv


class TestBezahltEinzeln:
    def test_contexted_invoice_books_without_dialog(self, client, admin, accounts):
        """Alle Positionen kontiert → ein Klick, eine normale Buchung."""
        a1, _a2, _ra = accounts
        client.get("/auth/logout")
        _login(client)
        inv_id = _sent_invoice("2024-00001", [
            {"amount": "80.00", "account_id": a1.id},
            {"amount": "20.00", "account_id": a1.id},
        ]).id

        r = client.post(f"/invoices/{inv_id}/status",
                        data={"status": Invoice.STATUS_PAID})
        assert r.status_code == 302

        inv = db.session.get(Invoice, inv_id)
        assert inv.status == Invoice.STATUS_PAID
        bookings = Booking.query.filter_by(invoice_id=inv_id).all()
        # Gleiches Konto, gleiches Projekt, gleicher Steuersatz -> KEINE Sammelbuchung
        assert len(bookings) == 1
        assert bookings[0].account_id == a1.id
        assert bookings[0].amount == Decimal("100.00")
        assert BookingGroup.query.count() == 0
        assert inv.open_item.status == OpenItem.STATUS_PAID

    def test_two_accounts_create_sammelbuchung(self, client, admin, accounts):
        a1, a2, _ra = accounts
        client.get("/auth/logout")
        _login(client)
        inv_id = _sent_invoice("2024-00002", [
            {"desc": "Wasser", "amount": "80.00", "account_id": a1.id},
            {"desc": "Grundgebühr", "amount": "20.00", "account_id": a2.id},
        ]).id

        client.post(f"/invoices/{inv_id}/status", data={"status": Invoice.STATUS_PAID})

        bookings = Booking.query.filter_by(invoice_id=inv_id).all()
        assert len(bookings) == 2
        assert {b.account_id for b in bookings} == {a1.id, a2.id}
        assert BookingGroup.query.count() == 1

    def test_uncontexted_without_account_is_rejected(self, client, admin, accounts):
        """Ohne Kontierung und ohne Konto im Formular bleibt der Status stehen."""
        client.get("/auth/logout")
        _login(client)
        inv_id = _sent_invoice("2024-00003", [{"amount": "100.00"}]).id

        client.post(f"/invoices/{inv_id}/status", data={"status": Invoice.STATUS_PAID})

        inv = db.session.get(Invoice, inv_id)
        assert inv.status == Invoice.STATUS_SENT
        assert Booking.query.filter_by(invoice_id=inv_id).count() == 0

    def test_dialog_account_books_uncontexted_invoice(self, client, admin, accounts):
        """Das im Dialog gewaehlte Konto macht die Buchung moeglich."""
        a1, _a2, _ra = accounts
        client.get("/auth/logout")
        _login(client)
        inv_id = _sent_invoice("2024-00004", [{"amount": "100.00"}]).id

        client.post(f"/invoices/{inv_id}/status",
                    data={"status": Invoice.STATUS_PAID, "account_id": str(a1.id)})

        inv = db.session.get(Invoice, inv_id)
        assert inv.status == Invoice.STATUS_PAID
        bookings = Booking.query.filter_by(invoice_id=inv_id).all()
        assert len(bookings) == 1
        assert bookings[0].account_id == a1.id

    def test_detail_page_offers_dialog_only_when_needed(self, client, admin, accounts):
        a1, _a2, _ra = accounts
        client.get("/auth/logout")
        _login(client)
        uncontexted = _sent_invoice("2024-00005", [{"amount": "100.00"}]).id
        contexted = _sent_invoice("2024-00006",
                                  [{"amount": "100.00", "account_id": a1.id}]).id

        html_uncontexted = client.get(f"/invoices/{uncontexted}").get_data(as_text=True)
        html_contexted = client.get(f"/invoices/{contexted}").get_data(as_text=True)

        assert "payAccountModal" in html_uncontexted
        assert "payAccountModal" not in html_contexted


class TestBezahltSammelaktion:
    def test_bulk_paid_creates_bookings(self, client, admin, accounts):
        """Regression: die Sammelaktion hat den Status gesetzt, aber nichts gebucht."""
        a1, _a2, _ra = accounts
        client.get("/auth/logout")
        _login(client)
        i1 = _sent_invoice("2024-00010", [{"amount": "50.00", "account_id": a1.id}]).id
        i2 = _sent_invoice("2024-00011", [{"amount": "70.00", "account_id": a1.id}]).id

        client.post("/invoices/bulk-action",
                    data={"action": Invoice.STATUS_PAID,
                          "invoice_ids": [str(i1), str(i2)]})

        for inv_id, amount in ((i1, Decimal("50.00")), (i2, Decimal("70.00"))):
            inv = db.session.get(Invoice, inv_id)
            assert inv.status == Invoice.STATUS_PAID
            bookings = Booking.query.filter_by(invoice_id=inv_id).all()
            assert len(bookings) == 1
            assert bookings[0].amount == amount
            assert inv.open_item.status == OpenItem.STATUS_PAID

    def test_bulk_paid_skips_uncontexted_and_keeps_rest(self, client, admin, accounts):
        """Eine unkontierte Rechnung darf den ganzen Stapel nicht zurueckrollen."""
        a1, _a2, _ra = accounts
        client.get("/auth/logout")
        _login(client)
        ok = _sent_invoice("2024-00020", [{"amount": "50.00", "account_id": a1.id}]).id
        bad = _sent_invoice("2024-00021", [{"amount": "70.00"}]).id

        client.post("/invoices/bulk-action",
                    data={"action": Invoice.STATUS_PAID,
                          "invoice_ids": [str(ok), str(bad)]})

        assert db.session.get(Invoice, ok).status == Invoice.STATUS_PAID
        assert Booking.query.filter_by(invoice_id=ok).count() == 1
        # Uebersprungen: Status unveraendert, keine Buchung
        assert db.session.get(Invoice, bad).status == Invoice.STATUS_SENT
        assert Booking.query.filter_by(invoice_id=bad).count() == 0

    def test_bulk_paid_uses_form_account_for_uncontexted(self, client, admin, accounts):
        a1, _a2, _ra = accounts
        client.get("/auth/logout")
        _login(client)
        inv_id = _sent_invoice("2024-00030", [{"amount": "60.00"}]).id

        client.post("/invoices/bulk-action",
                    data={"action": Invoice.STATUS_PAID,
                          "account_id": str(a1.id),
                          "invoice_ids": [str(inv_id)]})

        assert db.session.get(Invoice, inv_id).status == Invoice.STATUS_PAID
        bookings = Booking.query.filter_by(invoice_id=inv_id).all()
        assert len(bookings) == 1
        assert bookings[0].account_id == a1.id

    def test_bulk_paid_skips_invalid_transition(self, client, admin, accounts):
        """Ein Entwurf kann nicht direkt bezahlt werden (State-Machine)."""
        a1, _a2, _ra = accounts
        client.get("/auth/logout")
        _login(client)
        inv = _sent_invoice("2024-00040", [{"amount": "40.00", "account_id": a1.id}])
        inv.status = Invoice.STATUS_DRAFT
        db.session.commit()
        inv_id = inv.id

        client.post("/invoices/bulk-action",
                    data={"action": Invoice.STATUS_PAID, "invoice_ids": [str(inv_id)]})

        assert db.session.get(Invoice, inv_id).status == Invoice.STATUS_DRAFT
        assert Booking.query.filter_by(invoice_id=inv_id).count() == 0
