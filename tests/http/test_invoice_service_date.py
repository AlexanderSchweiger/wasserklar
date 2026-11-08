"""Liefer-/Leistungsdatum an der Rechnung (Pflichtangabe AT § 11 / DE § 14 UStG).

Einzelrechnungen tragen ihre eigene Angabe (Tag oder Zeitraum); Rechnungen aus der
Abrechnung folgen weiter der Periode.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.einvoice.mapper import build_einvoice
from app.extensions import db
from app.invoices.credit_note import create_credit_note
from app.invoices.services import service_display, service_period
from app.models import BillingPeriod, Customer, FiscalYear, Invoice, InvoiceItem, User
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
    row = BillingPeriod(name="2026", start_date=date(2026, 1, 1),
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
                  due_date=date(2026, 4, 5), status=Invoice.STATUS_DRAFT)
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(invoice_id=inv.id, description="Wasserzähler QN 2,5",
                               quantity=Decimal("1"), unit="Stk", unit_price=Decimal("80"),
                               amount=Decimal("80"), tax_rate=Decimal("20")))
    db.session.flush()
    inv.recalculate_total()
    db.session.commit()
    return inv


def _new_form(customer, **extra):
    form = {"customer_id": customer.id, "date": "2026-03-05", "due_date": "2026-04-05",
            "service_fields": "1", "service_mode": "day", "service_date_from": "2026-03-02",
            "service_type": "delivery"}
    form.update(extra)
    return form


def _edit_form(**extra):
    form = {"date": "2026-03-05", "due_date": "2026-04-05", "notes": "", "service_fields": "1",
            "service_mode": "day", "service_date_from": "2026-03-02", "service_type": "delivery"}
    form.update(extra)
    return form


class TestDisplay:
    def test_billing_period_stays_the_default(self, draft):
        assert service_period(draft) == (date(2026, 1, 1), date(2026, 12, 31))
        assert service_display(draft) == ("Leistungszeitraum", "01.01.2026 – 31.12.2026")

    def test_without_period_the_invoice_date_is_the_delivery_date(self, draft):
        draft.billing_period_id = None
        db.session.commit()
        assert service_display(draft) == ("Lieferdatum", "05.03.2026")

    @pytest.mark.parametrize("kind,end,label,text", [
        ("delivery", date(2026, 3, 2), "Lieferdatum", "02.03.2026"),
        ("service", date(2026, 3, 2), "Leistungsdatum", "02.03.2026"),
        ("delivery", date(2026, 3, 9), "Lieferzeitraum", "02.03.2026 – 09.03.2026"),
        ("service", date(2026, 3, 9), "Leistungszeitraum", "02.03.2026 – 09.03.2026"),
    ])
    def test_labels(self, draft, kind, end, label, text):
        draft.service_date_from, draft.service_date_to = date(2026, 3, 2), end
        draft.service_type = kind
        db.session.commit()
        assert service_display(draft) == (label, text)


class TestNewInvoice:
    def test_single_day_replaces_the_billing_period(self, logged_in, period, customer):
        logged_in.post("/invoices/new", data=_new_form(customer))
        inv = Invoice.query.one()
        assert inv.billing_period_id == period.id
        assert service_period(inv) == (date(2026, 3, 2), date(2026, 3, 2))
        assert service_display(inv) == ("Lieferdatum", "02.03.2026")

    def test_range(self, logged_in, period, customer):
        logged_in.post("/invoices/new", data=_new_form(
            customer, service_mode="range", service_date_to="2026-03-20", service_type="service"))
        inv = Invoice.query.one()
        assert service_display(inv) == ("Leistungszeitraum", "02.03.2026 – 20.03.2026")

    def test_without_the_fields_the_invoice_date_counts(self, logged_in, period, customer):
        logged_in.post("/invoices/new", data={
            "customer_id": customer.id, "date": "2026-03-05", "due_date": "2026-04-05"})
        assert service_period(Invoice.query.one()) == (date(2026, 3, 5), date(2026, 3, 5))

    @pytest.mark.parametrize("extra", [
        {"service_date_from": ""},
        {"service_mode": "range", "service_date_to": ""},
        {"service_mode": "range", "service_date_to": "2026-03-01"},
    ])
    def test_invalid_input_creates_nothing(self, logged_in, period, customer, extra):
        logged_in.post("/invoices/new", data=_new_form(customer, **extra))
        assert Invoice.query.count() == 0

    def test_form_offers_day_and_range(self, logged_in, period, customer):
        html = logged_in.get("/invoices/new").get_data(as_text=True)
        assert 'name="service_mode"' in html and 'name="service_date_to"' in html
        assert f'value="{date.today().isoformat()}"' in html


class TestEdit:
    def test_sets_a_range(self, logged_in, draft):
        logged_in.post(f"/invoices/{draft.id}/edit", data=_edit_form(
            service_mode="range", service_date_to="2026-03-09", service_type="service"))
        db.session.expire_all()
        assert service_period(draft) == (date(2026, 3, 2), date(2026, 3, 9))

    def test_keeping_the_billing_period_stores_nothing(self, logged_in, draft):
        logged_in.post(f"/invoices/{draft.id}/edit", data=_edit_form(
            service_mode="range", service_date_from="2026-01-01",
            service_date_to="2026-12-31", service_type="service"))
        db.session.expire_all()
        assert draft.service_date_from is None and draft.service_type is None

    def test_without_marker_nothing_changes(self, logged_in, draft):
        draft.service_date_from = draft.service_date_to = date(2026, 3, 2)
        db.session.commit()
        logged_in.post(f"/invoices/{draft.id}/edit",
                       data={"date": "2026-03-06", "due_date": "2026-04-05", "notes": ""})
        db.session.expire_all()
        assert draft.date == date(2026, 3, 6)
        assert draft.service_date_from == date(2026, 3, 2)

    def test_invalid_range_changes_nothing(self, logged_in, draft):
        logged_in.post(f"/invoices/{draft.id}/edit", data=_edit_form(
            service_mode="range", service_date_to="2026-03-01", notes="neu"))
        db.session.expire_all()
        assert draft.service_date_from is None and not draft.notes

    def test_form_shows_the_current_range(self, logged_in, draft):
        html = logged_in.get(f"/invoices/{draft.id}/edit").get_data(as_text=True)
        assert 'value="2026-01-01"' in html and 'value="2026-12-31"' in html

    def test_detail_page_shows_it(self, logged_in, draft):
        draft.service_date_from = draft.service_date_to = date(2026, 3, 2)
        draft.service_type = Invoice.SERVICE_TYPE_DELIVERY
        db.session.commit()
        html = logged_in.get(f"/invoices/{draft.id}").get_data(as_text=True)
        assert "Lieferdatum" in html and "02.03.2026" in html


class TestFollowUps:
    def test_einvoice_single_day_is_only_a_delivery_date(self, app, draft):
        draft.service_date_from = draft.service_date_to = date(2026, 3, 2)
        db.session.commit()
        einvoice = build_einvoice(draft)
        assert einvoice.period_start is None and einvoice.period_end is None
        assert einvoice.delivery.date == date(2026, 3, 2)

    def test_einvoice_range(self, app, draft):
        draft.service_date_from, draft.service_date_to = date(2026, 3, 2), date(2026, 3, 9)
        db.session.commit()
        einvoice = build_einvoice(draft)
        assert (einvoice.period_start, einvoice.period_end) == (date(2026, 3, 2), date(2026, 3, 9))
        assert einvoice.delivery.date == date(2026, 3, 9)

    def test_credit_note_takes_over_the_service_date(self, app, draft, admin):
        draft.service_date_from = draft.service_date_to = date(2026, 3, 2)
        draft.service_type = Invoice.SERVICE_TYPE_DELIVERY
        draft.status = Invoice.STATUS_SENT
        db.session.commit()
        credit = create_credit_note(draft, created_by_id=admin.id)
        assert service_period(credit) == (date(2026, 3, 2), date(2026, 3, 2))
        assert credit.service_type == Invoice.SERVICE_TYPE_DELIVERY
