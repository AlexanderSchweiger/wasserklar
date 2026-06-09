"""Sperrlisten-Gate beim Mailversand.

Zwei Schichten:
* **Route-Gate** (``/invoices/<id>/send-email-ajax``): gesperrte Empfaenger
  bekommen sofort eine 400 mit freundlicher Meldung, der Testversand an die
  Admin-Adresse bleibt erlaubt.
* **Chokepoint-Netz** (``settings_service.send_mail``): filtert gesperrte
  Empfaenger fuer JEDEN Versandpfad heraus, bevor ``mail.send`` laeuft.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db, mail
from app.models import (
    BillingPeriod, Customer, EmailSuppression, Invoice, InvoiceItem, User,
)
from app.email_suppression import suppress
from app.settings_service import send_mail
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.local", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post(
        "/auth/login", data={"username": "admin", "password": "secret"})


@pytest.fixture
def make_invoice(app):
    """Factory: Rechnung fuer einen Kunden mit gegebener (opt-in) E-Mail."""
    def _make(email):
        period = BillingPeriod(
            name="2024", start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31), active=True)
        db.session.add(period)
        cust = Customer(name="Kunde", customer_number=1, email=email,
                        rechnung_per_email=True)
        db.session.add(cust)
        db.session.flush()
        inv = Invoice(
            invoice_number="2024-00001", customer_id=cust.id,
            billing_period_id=period.id, date=date(2024, 12, 31),
            due_date=date(2025, 1, 31), status=Invoice.STATUS_DRAFT,
            total_amount=Decimal("100"))
        db.session.add(inv)
        db.session.flush()
        db.session.add(InvoiceItem(
            invoice_id=inv.id, description="Pos", quantity=Decimal("1"),
            unit="Stk", unit_price=Decimal("100"), amount=Decimal("100")))
        db.session.commit()
        return inv
    return _make


class TestRouteGate:
    def test_suppressed_recipient_returns_400(self, client, admin, make_invoice):
        client.get("/auth/logout")
        _login(client)
        inv = make_invoice("bad@example.test")
        suppress("bad@example.test", EmailSuppression.REASON_HARD_BOUNCE)
        db.session.commit()

        r = client.post(f"/invoices/{inv.id}/send-email-ajax", data={})
        assert r.status_code == 400
        assert "gesperrt" in r.get_json()["error"]

    def test_test_mode_bypasses_suppression(self, client, admin, make_invoice):
        """Testversand geht an die Admin-Adresse — die Kundensperre greift
        nicht (die Antwort darf nicht die Sperr-400 sein)."""
        client.get("/auth/logout")
        _login(client)
        inv = make_invoice("bad@example.test")
        suppress("bad@example.test", EmailSuppression.REASON_HARD_BOUNCE)
        db.session.commit()

        r = client.post(f"/invoices/{inv.id}/send-email-ajax",
                        data={"test_mode": "1"})
        # Nicht die Sperr-Antwort (je nach WeasyPrint 503 oder 200 — egal,
        # Hauptsache kein 400 "gesperrt").
        if r.status_code == 400:
            assert "gesperrt" not in (r.get_json() or {}).get("error", "")


class TestChokepointNet:
    def test_blocks_suppressed_recipient(self, app):
        from flask_mail import Message
        suppress("dead@example.test", EmailSuppression.REASON_HARD_BOUNCE)
        db.session.commit()
        with mail.record_messages() as outbox:
            send_mail(Message("Betreff", recipients=["dead@example.test"], body="x"))
        assert outbox == []

    def test_allows_clean_recipient(self, app):
        from flask_mail import Message
        with mail.record_messages() as outbox:
            send_mail(Message("Betreff", recipients=["live@example.test"], body="x"))
        assert len(outbox) == 1
        assert outbox[0].recipients == ["live@example.test"]

    def test_filters_only_suppressed_from_mixed(self, app):
        from flask_mail import Message
        suppress("dead@example.test", EmailSuppression.REASON_HARD_BOUNCE)
        db.session.commit()
        with mail.record_messages() as outbox:
            send_mail(Message(
                "Betreff",
                recipients=["dead@example.test", "live@example.test"],
                body="x"))
        assert len(outbox) == 1
        assert outbox[0].recipients == ["live@example.test"]
