"""Resend einer bereits Bezahlt/Guthaben-Rechnung darf den Status NICHT
auf "Versendet" zurueckregressieren (`_record_invoice_sent`).

Die "Per E-Mail versenden"-Aktion ist auf der Detailseite unabhaengig vom
Status verfuegbar (z.B. um dem Kunden eine verlorene Kopie erneut zu
schicken). Vor dem Fix setzte ``_record_invoice_sent`` unbedingt
``invoice.status = STATUS_SENT`` — dadurch "verlor" eine bezahlte Rechnung
ihren Bezahlt-Status, ohne dass Booking/OpenItem rueckabgewickelt wurden.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Account, AppSetting, BillingPeriod, Booking, Customer, FiscalYear,
    Invoice, InvoiceItem, OpenItem, RealAccount, User,
)
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


def _set(client, inv_id, status, **extra):
    data = {"status": status}
    data.update(extra)
    return client.post(f"/invoices/{inv_id}/status", data=data, follow_redirects=True)


@pytest.fixture
def paid_invoice(app):
    """Eine ausgestellte und bereits bezahlte Rechnung (100 netto + 20% USt),
    Kunde hat E-Mail-Versand aktiviert. Dokumentformat auf docx, damit der
    Test ohne WeasyPrint laeuft."""
    AppSetting.set("invoice.document_format", "docx")
    period = BillingPeriod(name="2026", start_date=date(2026, 1, 1),
                           end_date=date(2026, 12, 31), active=True)
    db.session.add(period)
    today = date.today()
    db.session.add(FiscalYear(
        year=today.year, start_date=date(today.year, 1, 1),
        end_date=date(today.year, 12, 31), closed=False))
    cust = Customer(name="Kunde", customer_number=1, email="kunde@example.test",
                    rechnung_per_email=True)
    db.session.add(cust)
    acc = Account(name="Wasser", code="W01")
    db.session.add(acc)
    db.session.add(RealAccount(name="Bank", iban="AT00", is_default=True, active=True))
    db.session.flush()
    inv = Invoice(
        invoice_number="2026-00001", customer_id=cust.id,
        billing_period_id=period.id, date=date(2026, 3, 1),
        due_date=date(2026, 3, 31), status=Invoice.STATUS_DRAFT)
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="Wasserverbrauch", quantity=Decimal("40"),
        unit="m3", unit_price=Decimal("2.5"), amount=Decimal("100"),
        tax_rate=Decimal("20")))
    db.session.flush()
    inv.recalculate_total()
    db.session.commit()
    inv._acc_id = acc.id
    return inv


class TestResendAfterPayment:
    @pytest.fixture(autouse=True)
    def _setup(self, client, admin, paid_invoice):
        _login(client)
        # Entwurf -> Versendet (legt den Offenen Posten an) -> Bezahlt
        _set(client, paid_invoice.id, Invoice.STATUS_SENT,
            account_id=str(paid_invoice._acc_id))
        _set(client, paid_invoice.id, Invoice.STATUS_PAID)
        self.iid = paid_invoice.id
        self.client = client
        db.session.expire_all()
        assert db.session.get(Invoice, self.iid).status == Invoice.STATUS_PAID

    def test_resend_keeps_paid_status(self):
        r = self.client.post(f"/invoices/{self.iid}/send-email-ajax", data={})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

        inv = db.session.get(Invoice, self.iid)
        assert inv.status == Invoice.STATUS_PAID

    def test_resend_does_not_touch_open_item_or_bookings(self):
        inv = db.session.get(Invoice, self.iid)
        oi_id = inv.open_item.id
        booking_count_before = Booking.query.filter_by(invoice_id=self.iid).count()
        assert booking_count_before >= 1

        r = self.client.post(f"/invoices/{self.iid}/send-email-ajax", data={})
        assert r.status_code == 200

        inv = db.session.get(Invoice, self.iid)
        assert inv.open_item.id == oi_id
        assert inv.open_item.status == OpenItem.STATUS_PAID
        assert Booking.query.filter_by(invoice_id=self.iid).count() == booking_count_before

    def test_resend_still_records_email_event(self):
        r = self.client.post(f"/invoices/{self.iid}/send-email-ajax", data={})
        assert r.status_code == 200

        r2 = self.client.get(f"/invoices/{self.iid}/email-events")
        assert r2.status_code == 200

    def test_resend_on_draft_still_transitions_to_sent(self, app):
        """Gegenprobe: der legitime Erstversand (Entwurf) transitioniert
        weiterhin normal auf Versendet."""
        cust = Customer(name="Kunde2", customer_number=2, email="k2@example.test",
                        rechnung_per_email=True)
        db.session.add(cust)
        db.session.flush()
        inv = Invoice(
            invoice_number="2026-00002", customer_id=cust.id,
            date=date(2026, 3, 1), due_date=date(2026, 3, 31),
            status=Invoice.STATUS_DRAFT)
        db.session.add(inv)
        db.session.flush()
        db.session.add(InvoiceItem(
            invoice_id=inv.id, description="Pos", quantity=Decimal("1"),
            unit="Stk", unit_price=Decimal("50"), amount=Decimal("50")))
        db.session.flush()
        inv.recalculate_total()
        db.session.commit()
        inv_id = inv.id

        r = self.client.post(f"/invoices/{inv_id}/send-email-ajax", data={})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

        assert db.session.get(Invoice, inv_id).status == Invoice.STATUS_SENT
