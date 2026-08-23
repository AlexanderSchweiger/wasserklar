"""HTTP-Tests fuer Konto & Bearbeitung am Offenen Posten (v1.43.0).

Zwei Quellen, klar getrennt:

* **Manueller Posten** — traegt sein eigenes ``account_id`` (im Anlage-/
  Bearbeiten-Modal waehlbar). Ist keines gesetzt, fragt der Ausgleich danach.
* **Posten aus einer Rechnung** — traegt NIE ein Konto; die Kontierung haengt
  an den Rechnungspositionen. Fehlt sie dort, fragt der Ausgleich ebenfalls.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account, Booking, BookingGroup, Customer, FiscalYear, Invoice, InvoiceItem,
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
    client.get("/auth/logout")
    return client.post("/auth/login",
                       data={"username": "admin", "password": "secret"})


@pytest.fixture
def base(app):
    """Kunde, zwei Konten, Bankkonto, offenes Buchungsjahr."""
    today = date.today()
    db.session.add(FiscalYear(
        year=today.year, start_date=date(today.year, 1, 1),
        end_date=date(today.year, 12, 31), closed=False))
    cust = Customer(name="Kunde", customer_number=1)
    a1 = Account(name="Wassererlöse", code="W01")
    a2 = Account(name="Anschlussgebühren", code="A01")
    db.session.add_all([cust, a1, a2,
                        RealAccount(name="Bank", iban="AT00",
                                    is_default=True, active=True)])
    db.session.commit()
    return {"customer": cust, "acc1": a1, "acc2": a2}


def _manual_op(customer_id, amount="100.00", account_id=None):
    op = OpenItem(customer_id=customer_id, description="Nachzahlung",
                  amount=Decimal(amount), date=date.today(),
                  status=OpenItem.STATUS_OPEN, account_id=account_id)
    db.session.add(op)
    db.session.commit()
    return op


def _invoice_op(customer_id, item_account_id=None, number="2026-00001"):
    """Versendete Rechnung + zugehoeriger Offener Posten (ohne Konto)."""
    inv = Invoice(invoice_number=number, customer_id=customer_id,
                  date=date.today(), due_date=date.today(),
                  status=Invoice.STATUS_SENT, total_amount=Decimal("100.00"))
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="Pos", quantity=Decimal("1"),
        unit="Stk", unit_price=Decimal("100"), amount=Decimal("100"),
        account_id=item_account_id))
    op = OpenItem(customer_id=customer_id, description=number,
                  amount=Decimal("100.00"), date=date.today(),
                  status=OpenItem.STATUS_OPEN, invoice_id=inv.id)
    db.session.add(op)
    db.session.commit()
    return inv, op


class TestManuellerPostenKonto:
    def test_new_modal_offers_account(self, client, admin, base):
        _login(client)
        html = client.get("/accounting/open-items/new",
                          headers={"X-From-Modal": "1"}).get_data(as_text=True)
        assert 'name="account_id"' in html
        assert "Wassererlöse" in html
        assert "beim Bezahlen fragen" in html

    def test_new_saves_account(self, client, admin, base):
        _login(client)
        acc_id = base["acc1"].id
        r = client.post("/accounting/open-items/new",
                        headers={"X-From-Modal": "1"},
                        data={"customer_id": str(base["customer"].id),
                              "description": "Anschluss",
                              "amount": "250,00",
                              "date": date.today().isoformat(),
                              "account_id": str(acc_id)})
        assert r.status_code == 204
        op = OpenItem.query.filter_by(description="Anschluss").one()
        assert op.account_id == acc_id
        assert op.amount == Decimal("250.00")

    def test_new_without_account_stays_open(self, client, admin, base):
        _login(client)
        client.post("/accounting/open-items/new",
                    headers={"X-From-Modal": "1"},
                    data={"customer_id": str(base["customer"].id),
                          "description": "Ohne Konto", "amount": "10,00",
                          "date": date.today().isoformat(), "account_id": ""})
        assert OpenItem.query.filter_by(description="Ohne Konto").one().account_id is None

    def test_negative_amount_allowed(self, client, admin, base):
        """Ein Rueckzahlungs-Posten ist ein negativer Betrag — nicht sperren."""
        _login(client)
        client.post("/accounting/open-items/new",
                    headers={"X-From-Modal": "1"},
                    data={"customer_id": str(base["customer"].id),
                          "description": "Rueckzahlung", "amount": "-40,00",
                          "date": date.today().isoformat()})
        assert OpenItem.query.filter_by(
            description="Rueckzahlung").one().amount == Decimal("-40.00")

    def test_zero_amount_rejected(self, client, admin, base):
        _login(client)
        r = client.post("/accounting/open-items/new",
                        headers={"X-From-Modal": "1"},
                        data={"customer_id": str(base["customer"].id),
                              "description": "Null", "amount": "0",
                              "date": date.today().isoformat()})
        assert r.status_code == 200          # Formular zurueck, kein 204
        assert OpenItem.query.filter_by(description="Null").count() == 0


class TestBearbeitenModal:
    def test_edit_updates_manual_item(self, client, admin, base):
        _login(client)
        op = _manual_op(base["customer"].id, account_id=base["acc1"].id)
        op_id, new_acc = op.id, base["acc2"].id
        r = client.post(f"/accounting/open-items/{op_id}/edit",
                        headers={"X-From-Modal": "1"},
                        data={"customer_id": str(base["customer"].id),
                              "description": "Geändert", "amount": "175,50",
                              "date": date.today().isoformat(),
                              "due_date": "2026-12-31",
                              "notes": "Notiz",
                              "account_id": str(new_acc)})
        assert r.status_code == 204
        op = db.session.get(OpenItem, op_id)
        assert op.description == "Geändert"
        assert op.amount == Decimal("175.50")
        assert op.account_id == new_acc
        assert op.due_date == date(2026, 12, 31)
        assert op.notes == "Notiz"

    def test_edit_of_invoice_item_keeps_beleg_fields(self, client, admin, base):
        """Bei einem Rechnungs-Posten sind Kunde/Betrag/Konto nicht aenderbar."""
        _login(client)
        _inv, op = _invoice_op(base["customer"].id, base["acc1"].id)
        op_id = op.id
        r = client.post(f"/accounting/open-items/{op_id}/edit",
                        headers={"X-From-Modal": "1"},
                        data={"customer_id": "999",
                              "description": "Hijack",
                              "amount": "1,00",
                              "account_id": str(base["acc2"].id),
                              "date": date.today().isoformat(),
                              "notes": "nur das hier zaehlt"})
        assert r.status_code == 204
        op = db.session.get(OpenItem, op_id)
        assert op.description == "2026-00001"        # unveraendert
        assert op.amount == Decimal("100.00")        # unveraendert
        assert op.account_id is None                 # nie ein eigenes Konto
        assert op.customer_id == base["customer"].id
        assert op.notes == "nur das hier zaehlt"     # editierbar

    def test_edit_modal_body_hides_account_for_invoice_item(self, client, admin, base):
        _login(client)
        _inv, op = _invoice_op(base["customer"].id, base["acc1"].id)
        html = client.get(f"/accounting/open-items/{op.id}/edit",
                          headers={"X-From-Modal": "1"}).get_data(as_text=True)
        assert 'name="account_id"' not in html
        assert "laut Rechnungspositionen" in html

    def test_row_endpoint_returns_single_row(self, client, admin, base):
        _login(client)
        op = _manual_op(base["customer"].id, account_id=base["acc1"].id)
        html = client.get(f"/accounting/open-items/{op.id}/row").get_data(as_text=True)
        assert f'id="open-item-row-{op.id}"' in html
        assert "<html" not in html.lower()


class TestAusgleichen:
    def test_manual_with_account_books_directly(self, client, admin, base):
        _login(client)
        op = _manual_op(base["customer"].id, account_id=base["acc1"].id)
        op_id, acc_id = op.id, base["acc1"].id
        client.post(f"/accounting/open-items/{op_id}/pay", data={"amount": "100.00"})
        b = Booking.query.filter_by(open_item_id=op_id).one()
        assert b.account_id == acc_id
        assert db.session.get(OpenItem, op_id).status == OpenItem.STATUS_PAID

    def test_manual_without_account_is_rejected(self, client, admin, base):
        """Kein stilles Ausweichen auf irgendein Konto mehr."""
        _login(client)
        op = _manual_op(base["customer"].id)
        op_id = op.id
        client.post(f"/accounting/open-items/{op_id}/pay", data={"amount": "100.00"})
        assert Booking.query.filter_by(open_item_id=op_id).count() == 0
        assert db.session.get(OpenItem, op_id).status == OpenItem.STATUS_OPEN

    def test_manual_without_account_uses_dialog_account(self, client, admin, base):
        _login(client)
        op = _manual_op(base["customer"].id)
        op_id, acc_id = op.id, base["acc2"].id
        client.post(f"/accounting/open-items/{op_id}/pay",
                    data={"amount": "100.00", "account_id": str(acc_id)})
        b = Booking.query.filter_by(open_item_id=op_id).one()
        assert b.account_id == acc_id
        # Das gewaehlte Konto bleibt am Posten haengen (manueller Posten).
        assert db.session.get(OpenItem, op_id).account_id == acc_id

    def test_invoice_op_uses_item_accounts(self, client, admin, base):
        _login(client)
        _inv, op = _invoice_op(base["customer"].id, base["acc1"].id)
        op_id, acc_id = op.id, base["acc1"].id
        client.post(f"/accounting/open-items/{op_id}/pay", data={"amount": "100.00"})
        b = Booking.query.filter_by(open_item_id=op_id).one()
        assert b.account_id == acc_id
        # Der Posten selbst bleibt kontenlos.
        assert db.session.get(OpenItem, op_id).account_id is None

    def test_invoice_op_without_item_account_needs_dialog(self, client, admin, base):
        _login(client)
        _inv, op = _invoice_op(base["customer"].id, None)
        op_id = op.id
        client.post(f"/accounting/open-items/{op_id}/pay", data={"amount": "100.00"})
        assert Booking.query.filter_by(open_item_id=op_id).count() == 0
        # ... mit Konto aus dem Dialog klappt es.
        client.post(f"/accounting/open-items/{op_id}/pay",
                    data={"amount": "100.00", "account_id": str(base["acc1"].id)})
        assert Booking.query.filter_by(open_item_id=op_id).count() == 1
        assert db.session.get(OpenItem, op_id).account_id is None


class TestListeUI:
    def test_no_bulk_account_action(self, client, admin, base):
        """Die Massen-Kontosetzung oben ist weg."""
        _login(client)
        html = client.get("/accounting/open-items").get_data(as_text=True)
        assert "Konto für alle setzen" not in html
        assert "set-account" not in html

    def test_row_without_account_opens_dialog(self, client, admin, base):
        """Ohne Kontierung wird der Bezahlen-Button zum Dialog-Oeffner."""
        _login(client)
        _manual_op(base["customer"].id)
        html = client.get("/accounting/open-items").get_data(as_text=True)
        assert 'onclick="openOpPayModal(this)"' in html
        assert "opPayAccountModal" in html

    def test_row_with_account_posts_directly(self, client, admin, base):
        """Mit Kontierung bleibt es beim Direkt-Submit (ein Klick)."""
        _login(client)
        _manual_op(base["customer"].id, account_id=base["acc1"].id)
        html = client.get("/accounting/open-items").get_data(as_text=True)
        assert 'onclick="openOpPayModal(this)"' not in html
        assert 'name="amount"' in html

    def test_set_account_endpoint_gone(self, client, admin, base):
        _login(client)
        assert client.post("/accounting/open-items/set-account",
                           data={"account_id": "1"}).status_code == 404
