"""HTTP-Tests fuer den Rechnungs-Lebenszyklus: Statuswechsel-State-Machine,
Buchungs-Rueckabwicklung beim Storno, Doppelbuchungs-Schutz und das Loeschen
von Entwuerfen.

Hintergrund: Frueher liess ``/invoices/<id>/status`` jeden Zielstatus zu. Das
fuehrte zu mehreren Problemen:
  * Entwurf -> Storniert war moeglich, obwohl ein nie ausgestellter Entwurf
    schlicht geloescht werden sollte.
  * Storno einer bezahlten Rechnung liess die automatische Zahlungsbuchung als
    Phantom-Einnahme stehen.
  * Bezahlt -> Versendet -> Bezahlt erzeugte eine zweite Buchung (Doppelbuchung).
  * Eine stornierte Rechnung liess sich beliebig reaktivieren.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.accounting import services as acc_svc
from app.extensions import db
from app.models import (
    Account, BillingPeriod, Booking, Customer, Invoice, InvoiceItem,
    InvoiceCounter, OpenItem, RealAccount, User,
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
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


@pytest.fixture
def draft(app):
    """Entwurfs-Rechnung ueber 100 EUR mit offenem Buchungsjahr + Standard-Bankkonto."""
    from app.models import FiscalYear
    period = BillingPeriod(
        name="2024", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31),
        active=True)
    db.session.add(period)
    # Offenes Buchungsjahr fuer das heutige Datum (Buchungs-/Storno-Datum).
    today = date.today()
    db.session.add(FiscalYear(
        year=today.year, start_date=date(today.year, 1, 1),
        end_date=date(today.year, 12, 31), closed=False))
    cust = Customer(name="Kunde", customer_number=1)
    db.session.add(cust)
    acc = Account(name="Wasser", code="W01")
    db.session.add(acc)
    db.session.add(RealAccount(name="Bank", iban="AT00", is_default=True, active=True))
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
    inv._acc_id = acc.id
    return inv


def _set(client, inv_id, status, **extra):
    data = {"status": status}
    data.update(extra)
    return client.post(f"/invoices/{inv_id}/status", data=data, follow_redirects=True)


def _active_bookings(inv_id):
    """Wirksame (nicht stornierte) Buchungen einer Rechnung."""
    return (
        Booking.query
        .filter(Booking.invoice_id == inv_id)
        .filter(acc_svc.storno_filter())
        .all()
    )


# ---------------------------------------------------------------------------
# State-Machine: erlaubte / verbotene Wechsel
# ---------------------------------------------------------------------------

class TestTransitionGuards:
    def test_draft_to_cancelled_blocked(self, client, admin, draft):
        """Ein Entwurf wird nicht storniert, sondern geloescht."""
        _login(client)
        iid = draft.id
        _set(client, iid, Invoice.STATUS_CANCELLED)
        assert db.session.get(Invoice, iid).status == Invoice.STATUS_DRAFT

    def test_draft_to_paid_blocked(self, client, admin, draft):
        """Ein Entwurf muss erst versendet werden, bevor er bezahlt sein kann."""
        _login(client)
        iid = draft.id
        _set(client, iid, Invoice.STATUS_PAID)
        inv = db.session.get(Invoice, iid)
        assert inv.status == Invoice.STATUS_DRAFT
        assert _active_bookings(iid) == []

    def test_draft_to_sent_allowed(self, client, admin, draft):
        _login(client)
        iid = draft.id
        _set(client, iid, Invoice.STATUS_SENT, account_id=str(draft._acc_id))
        inv = db.session.get(Invoice, iid)
        assert inv.status == Invoice.STATUS_SENT
        assert inv.open_item is not None

    def test_cancelled_is_terminal(self, client, admin, draft):
        """Eine stornierte Rechnung laesst sich nicht reaktivieren."""
        _login(client)
        iid = draft.id
        _set(client, iid, Invoice.STATUS_SENT, account_id=str(draft._acc_id))
        _set(client, iid, Invoice.STATUS_CANCELLED)
        assert db.session.get(Invoice, iid).status == Invoice.STATUS_CANCELLED
        # Reaktivierungs-Versuche prallen ab.
        for target in (Invoice.STATUS_SENT, Invoice.STATUS_PAID, Invoice.STATUS_CREDIT):
            _set(client, iid, target)
            assert db.session.get(Invoice, iid).status == Invoice.STATUS_CANCELLED

    def test_no_revert_to_draft(self, client, admin, draft):
        _login(client)
        iid = draft.id
        _set(client, iid, Invoice.STATUS_SENT, account_id=str(draft._acc_id))
        _set(client, iid, Invoice.STATUS_DRAFT)
        assert db.session.get(Invoice, iid).status == Invoice.STATUS_SENT


# ---------------------------------------------------------------------------
# Buchungs-Effekte
# ---------------------------------------------------------------------------

class TestBookingSideEffects:
    def test_paid_creates_single_booking(self, client, admin, draft):
        _login(client)
        iid = draft.id
        _set(client, iid, Invoice.STATUS_SENT, account_id=str(draft._acc_id))
        _set(client, iid, Invoice.STATUS_PAID)
        inv = db.session.get(Invoice, iid)
        assert inv.status == Invoice.STATUS_PAID
        assert len(_active_bookings(iid)) == 1
        assert inv.open_item.status == OpenItem.STATUS_PAID

    def test_no_double_booking_on_repaid(self, client, admin, draft):
        """Bezahlt -> Versendet -> Bezahlt darf keine zweite Buchung erzeugen."""
        _login(client)
        iid = draft.id
        acc_id = str(draft._acc_id)
        _set(client, iid, Invoice.STATUS_SENT, account_id=acc_id)
        _set(client, iid, Invoice.STATUS_PAID)
        _set(client, iid, Invoice.STATUS_SENT, account_id=acc_id)
        _set(client, iid, Invoice.STATUS_PAID)
        assert len(_active_bookings(iid)) == 1

    def test_cancel_reverses_booking(self, client, admin, draft):
        """Storno einer bezahlten Rechnung wickelt die Buchung ab (keine Phantom-Einnahme)."""
        _login(client)
        iid = draft.id
        _set(client, iid, Invoice.STATUS_SENT, account_id=str(draft._acc_id))
        _set(client, iid, Invoice.STATUS_PAID)
        assert len(_active_bookings(iid)) == 1
        _set(client, iid, Invoice.STATUS_CANCELLED)
        inv = db.session.get(Invoice, iid)
        assert inv.status == Invoice.STATUS_CANCELLED
        # Original storniert, Storno-Partner hebt es auf -> Netto 0 wirksam.
        assert _active_bookings(iid) == []
        total = sum((b.amount for b in Booking.query.filter_by(invoice_id=iid).all()),
                    Decimal("0"))
        assert total == Decimal("0")

    def test_cancel_sent_without_booking(self, client, admin, draft):
        """Storno einer nur versendeten (unbezahlten) Rechnung: OP wird geschlossen."""
        _login(client)
        iid = draft.id
        _set(client, iid, Invoice.STATUS_SENT, account_id=str(draft._acc_id))
        _set(client, iid, Invoice.STATUS_CANCELLED)
        inv = db.session.get(Invoice, iid)
        assert inv.status == Invoice.STATUS_CANCELLED
        assert inv.open_item.status == OpenItem.STATUS_PAID


# ---------------------------------------------------------------------------
# Loeschen von Entwuerfen
# ---------------------------------------------------------------------------

class TestDeleteDraft:
    def test_delete_draft_removes_invoice(self, client, admin, draft):
        _login(client)
        iid = draft.id
        r = client.post(f"/invoices/{iid}/delete", follow_redirects=True)
        assert r.status_code == 200
        assert db.session.get(Invoice, iid) is None
        assert InvoiceItem.query.filter_by(invoice_id=iid).count() == 0

    def test_delete_non_draft_blocked(self, client, admin, draft):
        _login(client)
        iid = draft.id
        _set(client, iid, Invoice.STATUS_SENT, account_id=str(draft._acc_id))
        client.post(f"/invoices/{iid}/delete", follow_redirects=True)
        assert db.session.get(Invoice, iid) is not None

    def test_delete_draft_resets_counter(self, client, admin, draft):
        _login(client)
        iid = draft.id
        db.session.add(InvoiceCounter(year=2024, next_seq=2))
        db.session.commit()
        client.post(f"/invoices/{iid}/delete", data={"reset_counter": "1"},
                    follow_redirects=True)
        counter = db.session.get(InvoiceCounter, 2024)
        # Keine Rechnungen 2024 mehr -> Zaehler zurueck auf 1.
        assert counter.next_seq == 1
