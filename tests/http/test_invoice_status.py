"""HTTP-Tests fuer den Rechnungs-Statuswechsel (``/invoices/<id>/status``).

Regression: Eine aus einem Rechnungslauf stammende Rechnung (``billing_run_id``
gesetzt) liess sich nicht auf 'Versendet' setzen, weil die Konto-Aufloesung auf
``billing_run.account_id`` zugriff — diese Spalte gab es da schon nicht mehr.

Seit v1.43.0 gibt es gar keine Konto-Aufloesung mehr: der Posten aus einer
Rechnung bleibt kontenlos, die Kontierung sitzt auf den Rechnungspositionen.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account, BillingPeriod, BillingRun, Customer, Invoice, InvoiceItem, User,
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
def billing_run_invoice(app):
    """Entwurfs-Rechnung mit verknuepftem Rechnungslauf (``billing_run_id`` gesetzt)."""
    period = BillingPeriod(
        name="2024", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
        active=True)
    db.session.add(period)
    cust = Customer(name="Kunde", customer_number=1)
    db.session.add(cust)
    db.session.flush()
    run = BillingRun(
        billing_period_id=period.id,
        tariff_name="T", tariff_price_per_m3=Decimal("2"))
    db.session.add(run)
    db.session.flush()
    inv = Invoice(
        invoice_number="2024-00001",
        customer_id=cust.id,
        billing_run_id=run.id,
        billing_period_id=period.id,
        date=date(2024, 12, 31),
        due_date=date(2025, 1, 31),
        status=Invoice.STATUS_DRAFT,
        total_amount=Decimal("100"),
    )
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="Pos", quantity=Decimal("1"),
        unit="Stk", unit_price=Decimal("100"), amount=Decimal("100")))
    db.session.commit()
    return inv


class TestSetStatusBillingRunInvoice:
    def test_draft_to_sent_creates_open_item(self, client, admin, billing_run_invoice):
        """Regression: Versenden einer Rechnungslauf-Rechnung darf nicht an
        ``billing_run.account_id`` scheitern."""
        client.get("/auth/logout")
        _login(client)
        inv_id = billing_run_invoice.id

        r = client.post(
            f"/invoices/{inv_id}/status",
            data={"status": Invoice.STATUS_SENT},
            follow_redirects=False)
        assert r.status_code == 302

        inv = db.session.get(Invoice, inv_id)
        assert inv.status == Invoice.STATUS_SENT
        # Offener Posten wurde angelegt; ohne Formular-Konto bleibt account_id offen.
        assert inv.open_item is not None
        assert inv.open_item.amount == Decimal("100")
        assert inv.open_item.account_id is None

    def test_sent_never_sets_open_item_account(self, client, admin,
                                               billing_run_invoice):
        """Ein Posten AUS EINER RECHNUNG traegt nie ein eigenes Buchungskonto.

        Die Kontierung haengt seit v1.43.0 an den Rechnungspositionen — ein
        Konto am Posten waere eine zweite, konkurrierende Wahrheit. Selbst ein
        mitgeschicktes ``account_id`` darf daran nichts aendern.
        """
        client.get("/auth/logout")
        _login(client)
        account = Account(name="Wasser", code="W01")
        db.session.add(account)
        db.session.commit()
        acc_id = account.id
        inv_id = billing_run_invoice.id

        r = client.post(
            f"/invoices/{inv_id}/status",
            data={"status": Invoice.STATUS_SENT, "account_id": str(acc_id)},
            follow_redirects=False)
        assert r.status_code == 302

        inv = db.session.get(Invoice, inv_id)
        assert inv.open_item is not None
        assert inv.open_item.account_id is None
