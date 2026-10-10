"""HTTP: MwSt im Positions-Editor (manuelle Rechnung, Entwurf bearbeiten) ist
eine Auswahl der hinterlegten Steuersätze, keine freie Eingabe — und der Server
nimmt nur hinterlegte Sätze an."""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import Customer, FiscalYear, Invoice, InvoiceItem, User
from tests.conftest import _ensure_role

TODAY = date.today()
D = Decimal


@pytest.fixture
def logged_in(app, client):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.add(FiscalYear(year=TODAY.year, start_date=date(TODAY.year, 1, 1),
                              end_date=date(TODAY.year, 12, 31), is_vat_liable=True))
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return client


@pytest.fixture
def customer(app):
    c = Customer(name="Huber Hans", customer_number=1)
    db.session.add(c)
    db.session.commit()
    return c


def _free_rows(*rows):
    """rows: (Beschreibung, Preis, MwSt)"""
    n = len(rows)
    return {
        "row_type[]": ["free"] * n, "row_tariff_id[]": [""] * n,
        "row_consumption_m3[]": [""] * n,
        "row_description[]": [r[0] for r in rows], "row_quantity[]": ["1"] * n,
        "row_unit[]": ["Stk"] * n, "row_unit_price[]": [r[1] for r in rows],
        "row_tax_rate[]": [r[2] for r in rows],
        "row_account_id[]": [""] * n, "row_project_id[]": [""] * n,
    }


def test_new_form_offers_select(logged_in):
    html = logged_in.get("/invoices/new").get_data(as_text=True)
    assert 'type="number" name="row_tax_rate[]"' not in html
    assert '<select name="row_tax_rate[]"' in html
    assert '<option value="20"' in html


def test_unknown_rate_is_dropped(logged_in, customer):
    resp = logged_in.post("/invoices/new", data={
        "customer_id": str(customer.id), "date": TODAY.isoformat(),
        **_free_rows(("Anschluss", "100", "20"), ("Sonder", "50", "17"))})
    assert resp.status_code == 302
    inv = Invoice.query.one()
    rates = {i.description: i.tax_rate for i in inv.items}
    assert rates == {"Anschluss": D("20.00"), "Sonder": None}


def test_draft_editor_keeps_legacy_rate(logged_in, customer):
    # Altposition mit einem Satz, der nicht (mehr) hinterlegt ist.
    inv = Invoice(customer_id=customer.id, date=TODAY, status=Invoice.STATUS_DRAFT,
                  invoice_number=f"{TODAY.year}-00001")
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(invoice_id=inv.id, description="Alt", quantity=D("1"),
                               unit="Stk", unit_price=D("10"), amount=D("10"), tax_rate=D("17")))
    db.session.commit()

    html = logged_in.get(f"/invoices/{inv.id}").get_data(as_text=True)
    assert '<option value="17.00" selected>' in html

    resp = logged_in.post(f"/invoices/{inv.id}/items/save",
                          data=_free_rows(("Alt", "10", "17")))
    assert resp.status_code == 302
    [item] = db.session.get(Invoice, inv.id).items
    assert item.tax_rate == D("17.00")
