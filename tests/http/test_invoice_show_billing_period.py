"""Abrechnungsperiode auf Einzelrechnungen nur auf Wunsch andrucken.

Die Periode einer manuell erstellten Rechnung ist nur aus dem Rechnungsdatum
abgeleitet (Einordnung in die Periodenuebersicht). Ob sie auf dem Beleg steht,
waehlt der Nutzer; Rechnungslauf, Schlussrechnung und Bestand (NULL) bleiben
beim Andrucken.
"""
import io
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.invoices.credit_note import create_credit_note
from app.invoices.pdf_service import render_invoice_html
from app.models import BillingPeriod, BillingRun, Customer, FiscalYear, Invoice, InvoiceItem, User
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    user = User(username="admin", email="admin@a.test", role_id=_ensure_role("Admin").id)
    user.set_password("secret")
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def logged_in(client, admin):
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "admin", "password": "secret"})
    return client


@pytest.fixture
def period(app):
    row = BillingPeriod(name="WJ 2026", start_date=date(2026, 1, 1),
                        end_date=date(2026, 12, 31), active=True)
    db.session.add_all([row, FiscalYear(year=2026, start_date=date(2026, 1, 1),
                                        end_date=date(2026, 12, 31))])
    db.session.commit()
    return row


@pytest.fixture
def customer(app):
    row = Customer(name="Kunde Test", customer_number=7)
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def draft(period, customer):
    inv = Invoice(invoice_number="2026-00050", customer_id=customer.id,
                  billing_period_id=period.id, date=date(2026, 3, 5),
                  due_date=date(2026, 4, 5), status=Invoice.STATUS_DRAFT,
                  show_billing_period=False)
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(invoice_id=inv.id, description="Wasserzähler QN 2,5",
                               quantity=Decimal("1"), unit="Stk", unit_price=Decimal("80"),
                               amount=Decimal("80")))
    db.session.flush()
    inv.recalculate_total()
    db.session.commit()
    return inv


def _new_form(customer, **extra):
    form = {"customer_id": customer.id, "date": "2026-03-05", "due_date": "2026-04-05",
            "billing_period_field": "1"}
    form.update(extra)
    return form


def _edit_form(**extra):
    form = {"date": "2026-03-05", "due_date": "2026-04-05", "notes": "",
            "billing_period_field": "1"}
    form.update(extra)
    return form


def _html(app, invoice):
    with app.test_request_context():
        return render_invoice_html(invoice)


def _docx_text(app, invoice):
    from docx import Document
    from app.invoices.document_service import generate_docx
    from app.settings_service import wg_settings
    with app.test_request_context():
        data = generate_docx(invoice, wg_settings())
    doc = Document(io.BytesIO(data))
    return " ".join(c.text for t in doc.tables for row in t.rows for c in row.cells)


class TestRule:
    def test_null_prints_like_before(self, draft):
        draft.show_billing_period = None
        assert draft.prints_billing_period

    def test_false_hides_it(self, draft):
        assert not draft.prints_billing_period

    def test_without_period_nothing_is_printed(self, draft):
        draft.billing_period_id = None
        draft.show_billing_period = True
        db.session.flush()
        db.session.expire(draft, ["billing_period"])
        assert not draft.prints_billing_period

    def test_choice_only_for_single_invoices(self, draft, period):
        assert draft.billing_period_choice
        run = BillingRun(billing_period_id=period.id, tariff_name="Standard")
        db.session.add(run)
        db.session.flush()
        draft.billing_run_id = run.id
        assert not draft.billing_period_choice
        draft.billing_run_id = None
        draft.invoice_kind = Invoice.KIND_FINAL_SETTLEMENT
        assert not draft.billing_period_choice


class TestNewInvoice:
    def test_default_is_off(self, logged_in, period, customer):
        logged_in.post("/invoices/new", data=_new_form(customer))
        inv = Invoice.query.one()
        assert inv.billing_period_id == period.id      # Einordnung bleibt
        assert inv.show_billing_period is False

    def test_switched_on(self, logged_in, period, customer):
        logged_in.post("/invoices/new", data=_new_form(customer, show_billing_period="1"))
        assert Invoice.query.one().show_billing_period is True

    def test_form_offers_the_switch_unchecked(self, logged_in, period, customer):
        html = logged_in.get("/invoices/new").get_data(as_text=True)
        assert 'name="show_billing_period"' in html
        start = html.index('name="show_billing_period"')
        assert "checked" not in html[start:html.index(">", start)]


class TestEdit:
    def test_switch_on_and_off(self, logged_in, draft):
        logged_in.post(f"/invoices/{draft.id}/edit", data=_edit_form(show_billing_period="1"))
        db.session.expire_all()
        assert draft.show_billing_period is True
        logged_in.post(f"/invoices/{draft.id}/edit", data=_edit_form())
        db.session.expire_all()
        assert draft.show_billing_period is False

    def test_without_marker_nothing_changes(self, logged_in, draft):
        draft.show_billing_period = True
        db.session.commit()
        logged_in.post(f"/invoices/{draft.id}/edit",
                       data={"date": "2026-03-05", "due_date": "2026-04-05", "notes": ""})
        db.session.expire_all()
        assert draft.show_billing_period is True

    def test_form_shows_the_current_choice(self, logged_in, draft):
        draft.show_billing_period = True
        db.session.commit()
        html = logged_in.get(f"/invoices/{draft.id}/edit").get_data(as_text=True)
        start = html.index('name="show_billing_period"')
        assert "checked" in html[start:html.index(">", start)]
        assert "WJ 2026" in html

    def test_run_invoices_get_no_switch(self, logged_in, draft, period):
        run = BillingRun(billing_period_id=period.id, tariff_name="Standard")
        db.session.add(run)
        db.session.flush()
        draft.billing_run_id = run.id
        draft.show_billing_period = None
        db.session.commit()
        html = logged_in.get(f"/invoices/{draft.id}/edit").get_data(as_text=True)
        assert 'name="show_billing_period"' not in html
        logged_in.post(f"/invoices/{draft.id}/edit", data=_edit_form())
        db.session.expire_all()
        assert draft.show_billing_period is None


class TestDocuments:
    def test_pdf_hides_the_period(self, app, draft):
        assert "Abrechnungsperiode" not in _html(app, draft)

    def test_pdf_shows_it_when_wanted(self, app, draft):
        draft.show_billing_period = True
        db.session.commit()
        html = _html(app, draft)
        assert "Abrechnungsperiode" in html and "WJ 2026" in html

    def test_word_follows_the_switch(self, app, draft):
        assert "Abrechnungsperiode" not in _docx_text(app, draft)
        draft.show_billing_period = None
        db.session.commit()
        assert "Abrechnungsperiode" in _docx_text(app, draft)

    def test_detail_page_says_not_printed(self, logged_in, draft):
        html = logged_in.get(f"/invoices/{draft.id}").get_data(as_text=True)
        assert "nicht angedruckt" in html

    def test_credit_note_takes_over_the_choice(self, app, draft, admin):
        draft.status = Invoice.STATUS_SENT
        db.session.commit()
        credit = create_credit_note(draft, created_by_id=admin.id)
        assert credit.show_billing_period is False
        assert credit.billing_period_choice
