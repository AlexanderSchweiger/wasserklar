"""HTTP-Tests fuer die Storno-Rechnung (Gutschrift).

Fachlicher Kern: eine ausgestellte Rechnung wird nie geloescht oder
ueberschrieben — das Storno ist ein **zweiter Beleg** mit eigener Nummer und
gespiegelten Positionen. War die Rechnung bereits bezahlt, entsteht zusaetzlich
ein negativer Offener Posten, der die Rueckzahlungspflicht abbildet; war sie
unbezahlt, darf genau das NICHT passieren (sonst taeuscht die App eine
Verbindlichkeit vor, die es nicht gibt).
"""
from datetime import date
from decimal import Decimal

import pytest

from app.accounting import services as acc_svc
from app.extensions import db
from app.models import (
    Account, BillingPeriod, Booking, Customer, FiscalYear, Invoice, InvoiceItem,
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
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


@pytest.fixture
def draft(app):
    """Entwurf ueber 120 EUR (100 netto + 20 % USt) mit offenem Buchungsjahr."""
    period = BillingPeriod(name="2026", start_date=date(2026, 1, 1),
                           end_date=date(2026, 12, 31), active=True)
    db.session.add(period)
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
        invoice_number="2026-00001", customer_id=cust.id,
        billing_period_id=period.id, date=date(2026, 3, 1),
        due_date=date(2026, 3, 31), status=Invoice.STATUS_DRAFT)
    db.session.add(inv)
    db.session.flush()
    db.session.add(InvoiceItem(
        invoice_id=inv.id, description="Wasserverbrauch", quantity=Decimal("40"),
        unit="m3", unit_price=Decimal("2.5"), amount=Decimal("100"),
        tax_rate=Decimal("20"),
        # Kontierung seit v1.43.0 an der Position — der Offene Posten aus einer
        # Rechnung traegt kein eigenes Konto mehr.
        account_id=acc.id))
    db.session.flush()
    inv.recalculate_total()
    db.session.commit()
    inv._acc_id = acc.id
    return inv


def _set(client, inv_id, status, **extra):
    data = {"status": status}
    data.update(extra)
    return client.post(f"/invoices/{inv_id}/status", data=data, follow_redirects=True)


def _issue(client, inv):
    """Entwurf -> Versendet (legt den Offenen Posten an)."""
    return _set(client, inv.id, Invoice.STATUS_SENT)


def _credit_note_of(invoice_id):
    return Invoice.query.filter_by(cancels_invoice_id=invoice_id).first()


def _issue_credit_note(client, invoice_id):
    """Gutschrift-Entwurf auf „Versendet" setzen — dabei entsteht der
    Rueckzahlungs-Posten (analog zur normalen Rechnung)."""
    cn = _credit_note_of(invoice_id)
    assert cn is not None
    _set(client, cn.id, Invoice.STATUS_SENT)
    return db.session.get(Invoice, cn.id)


# ---------------------------------------------------------------------------
# Storno einer BEZAHLTEN Rechnung -> Gutschrift + negativer Offener Posten
# ---------------------------------------------------------------------------

class TestCreditNoteForPaidInvoice:
    @pytest.fixture(autouse=True)
    def _paid(self, client, admin, draft):
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_PAID)
        self.iid = draft.id
        self.client = client

    def test_credit_note_created_with_own_number(self):
        _set(self.client, self.iid, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _credit_note_of(self.iid)
        assert cn is not None
        assert cn.invoice_kind == Invoice.KIND_CREDIT_NOTE
        assert cn.invoice_number != "2026-00001"
        # Entwurf: der Nutzer prueft und versendet selbst (per Mail/Ausdruck).
        assert cn.status == Invoice.STATUS_DRAFT

    def test_original_stays_cancelled_and_untouched(self):
        _set(self.client, self.iid, Invoice.STATUS_CANCELLED, create_credit_note="1")
        original = db.session.get(Invoice, self.iid)
        assert original.status == Invoice.STATUS_CANCELLED
        assert original.total_amount == Decimal("120.00")
        assert len(original.items) == 1

    def test_items_are_mirrored_negative(self):
        _set(self.client, self.iid, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _credit_note_of(self.iid)
        assert cn.total_amount == Decimal("-120.00")
        item = cn.items[0]
        assert item.amount == Decimal("-100")
        assert item.quantity == Decimal("-40")
        # Einzelpreis bleibt positiv — die Zeile liest sich als Rueckabwicklung.
        assert item.unit_price == Decimal("2.5")
        assert item.tax_rate == Decimal("20")

    def test_draft_has_no_open_item_yet(self):
        """Der Entwurf begruendet noch keine Verbindlichkeit."""
        _set(self.client, self.iid, Invoice.STATUS_CANCELLED, create_credit_note="1")
        assert _credit_note_of(self.iid).open_item is None

    def test_negative_open_item_on_send(self):
        _set(self.client, self.iid, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _issue_credit_note(self.client, self.iid)
        assert cn.status == Invoice.STATUS_SENT
        assert cn.open_item is not None
        assert cn.open_item.amount == Decimal("-120.00")
        assert cn.open_item.status == OpenItem.STATUS_OPEN

    def test_refund_is_derived_from_kept_payment(self):
        """Der Betrag wird abgeleitet, nicht gespeichert — er ist die wirksam
        auf die Originalrechnung gebuchte Zahlung."""
        from app.invoices.credit_note import refund_amount_for
        _set(self.client, self.iid, Invoice.STATUS_CANCELLED, create_credit_note="1")
        assert refund_amount_for(_credit_note_of(self.iid)) == Decimal("120.00")

    def test_payment_booking_stays(self):
        """Regression: der Storno nahm die echte Zahlung mit zurueck — mit der
        Rueckueberweisung stand der Betrag dann doppelt als Ausgabe in der
        Buchhaltung."""
        _set(self.client, self.iid, Invoice.STATUS_CANCELLED, create_credit_note="1")
        active = (Booking.query.filter(Booking.invoice_id == self.iid)
                  .filter(acc_svc.storno_filter()).all())
        assert sum(b.amount for b in active) == Decimal("120.00")
        assert Booking.query.filter(Booking.storno_of_id.isnot(None)).count() == 0

    def test_payment_not_received_reverses_and_skips_refund(self):
        """Irrtuemlich als bezahlt erfasst: Zahlung wird mitstorniert, die
        Gutschrift begruendet dann keine Rueckzahlung."""
        _set(self.client, self.iid, Invoice.STATUS_CANCELLED,
             create_credit_note="1", payment_not_received="1")
        active = (Booking.query.filter(Booking.invoice_id == self.iid)
                  .filter(acc_svc.storno_filter()).all())
        assert active == []
        cn = _issue_credit_note(self.client, self.iid)
        assert cn.open_item is None
        assert cn.refund_amount == Decimal("0")

    def test_without_credit_note_payment_kept_with_hint(self):
        resp = _set(self.client, self.iid, Invoice.STATUS_CANCELLED)
        page = resp.get_data(as_text=True)
        assert "bleibt als Einnahme" in page
        assert db.session.get(Invoice, self.iid).paid_amount == Decimal("120.00")

    def test_storno_dialog_offers_not_received_switch(self):
        page = self.client.get(f"/invoices/{self.iid}").get_data(as_text=True)
        assert 'name="payment_not_received"' in page
        assert "Die Zahlung ist nie eingegangen" in page

    def test_no_credit_note_without_opt_in(self):
        _set(self.client, self.iid, Invoice.STATUS_CANCELLED)
        assert _credit_note_of(self.iid) is None
        assert db.session.get(Invoice, self.iid).status == Invoice.STATUS_CANCELLED

    def test_second_credit_note_blocked(self):
        _set(self.client, self.iid, Invoice.STATUS_CANCELLED, create_credit_note="1")
        first = _credit_note_of(self.iid).id
        self.client.post(f"/invoices/{self.iid}/credit-note",
                         data={}, follow_redirects=True)
        notes = Invoice.query.filter_by(cancels_invoice_id=self.iid).all()
        assert [n.id for n in notes] == [first]


# ---------------------------------------------------------------------------
# Storno einer UNBEZAHLTEN Rechnung -> Beleg, aber kein Rueckzahlungs-Posten
# ---------------------------------------------------------------------------

class TestCreditNoteForUnpaidInvoice:
    def test_no_open_item_when_nothing_was_paid(self, client, admin, draft):
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _credit_note_of(draft.id)
        assert cn is not None
        assert cn.open_item is None

    def test_original_open_item_closed(self, client, admin, draft):
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        assert db.session.get(Invoice, draft.id).open_item.status == OpenItem.STATUS_PAID


# ---------------------------------------------------------------------------
# Nachtraegliches Ausstellen + Guards
# ---------------------------------------------------------------------------

class TestCreditNoteRoute:
    def test_requires_cancelled_invoice(self, client, admin, draft):
        _login(client)
        _issue(client, draft)
        client.post(f"/invoices/{draft.id}/credit-note",
                    data={}, follow_redirects=True)
        assert _credit_note_of(draft.id) is None

    def test_creates_note_after_the_fact(self, client, admin, draft):
        """Storno ohne Gutschrift, spaeter nachgeholt.

        Der Rueckzahlbetrag ist auch hier ableitbar: die Zahlung ist beim
        Storno stehen geblieben.
        """
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_PAID)
        _set(client, draft.id, Invoice.STATUS_CANCELLED)
        assert _credit_note_of(draft.id) is None
        client.post(f"/invoices/{draft.id}/credit-note",
                    data={}, follow_redirects=True)
        cn = _credit_note_of(draft.id)
        assert cn is not None
        assert cn.status == Invoice.STATUS_DRAFT
        cn = _issue_credit_note(client, draft.id)
        assert cn.open_item.amount == Decimal("-120.00")

    def test_draft_credit_note_is_deleted_not_cancelled(self, client, admin, draft):
        """Die Gutschrift ist ein Entwurf — fuer den gilt dieselbe Regel wie
        ueberall: loeschen, nicht stornieren."""
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_PAID)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _credit_note_of(draft.id)
        assert cn.status == Invoice.STATUS_DRAFT

        # Storno prallt ab ...
        _set(client, cn.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        assert db.session.get(Invoice, cn.id).status == Invoice.STATUS_DRAFT

        # ... loeschen geht. Danach ist wieder eine neue Gutschrift moeglich.
        client.post(f"/invoices/{cn.id}/delete", follow_redirects=True)
        assert db.session.get(Invoice, cn.id) is None
        assert _credit_note_of(draft.id) is None

    def test_no_credit_note_on_a_sent_credit_note(self, client, admin, draft):
        """Eine versendete Gutschrift kann storniert werden — aber sie bekommt
        keine Gutschrift-der-Gutschrift."""
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_PAID)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _issue_credit_note(client, draft.id)
        _set(client, cn.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        assert db.session.get(Invoice, cn.id).status == Invoice.STATUS_CANCELLED
        assert _credit_note_of(cn.id) is None


# ---------------------------------------------------------------------------
# Beleg-Darstellung
# ---------------------------------------------------------------------------

class TestCreditNoteDocument:
    def test_detail_page_links_both_directions(self, client, admin, draft):
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_PAID)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _credit_note_of(draft.id)

        page = client.get(f"/invoices/{cn.id}").get_data(as_text=True)
        assert "Storno-Rechnung (Gutschrift)" in page
        assert "2026-00001" in page

        original_page = client.get(f"/invoices/{draft.id}").get_data(as_text=True)
        assert cn.invoice_number in original_page

    def test_document_title(self, client, admin, draft):
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        assert _credit_note_of(draft.id).document_title == "Gutschrift"
        assert db.session.get(Invoice, draft.id).document_title == "Rechnung"

    def test_period_overview_excludes_credit_note(self, client, admin, draft):
        """Die stornierte Rechnung ist in den Summen schon ausgeblendet — die
        Gutschrift darf denselben Betrag nicht ein zweites Mal abziehen."""
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _credit_note_of(draft.id)
        period_id = db.session.get(Invoice, draft.id).billing_period_id
        page = client.get(f"/invoices/period/{period_id}").get_data(as_text=True)
        assert cn.invoice_number not in page


# ---------------------------------------------------------------------------
# Beleg-Rendering (PDF-HTML + Word) — laeuft ueber eigene Template-Zweige
# ---------------------------------------------------------------------------

class TestCreditNoteRendering:
    def _credit_note(self, client, draft, *, paid=True):
        """Gutschrift erzeugen UND versenden — das PDF zeigt den
        Rueckzahlungsblock erst, wenn der Posten existiert."""
        _login(client)
        _issue(client, draft)
        if paid:
            _set(client, draft.id, Invoice.STATUS_PAID)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        return _issue_credit_note(client, draft.id)

    def test_pdf_html_uses_credit_wording(self, app, client, admin, draft):
        from app.invoices.routes import _render_pdf_html
        cn = self._credit_note(client, draft)
        with app.test_request_context():
            html = _render_pdf_html(cn)
        assert "Gutschrift" in html
        assert "Storno zu Rechnung" in html
        assert "2026-00001" in html
        assert "Rueckzahlung" in html or "Rückzahlung" in html
        # Kein Zahlungsaufruf auf einer Gutschrift.
        assert "auf unser Konto einzuzahlen" not in html

    def test_pdf_html_without_refund(self, app, client, admin, draft):
        from app.invoices.routes import _render_pdf_html
        cn = self._credit_note(client, draft, paid=False)
        with app.test_request_context():
            html = _render_pdf_html(cn)
        assert "kein Rückzahlungsbetrag" in html

    def test_docx_renders(self, app, client, admin, draft):
        from app.invoices.document_service import generate_docx
        from app.settings_service import wg_settings
        cn = self._credit_note(client, draft)
        with app.test_request_context():
            data = generate_docx(cn, wg_settings())
        assert data[:2] == b"PK"   # gueltiges .docx (ZIP-Container)

    def test_original_invoice_still_renders_normally(self, app, client, admin, draft):
        from app.invoices.routes import _render_pdf_html
        self._credit_note(client, draft)
        with app.test_request_context():
            html = _render_pdf_html(db.session.get(Invoice, draft.id))
        assert "auf unser Konto einzuzahlen" in html

    def test_cancelled_invoice_shows_refund(self, client, admin, draft):
        """Nach dem Storno steht die Zahlung noch — der Nachtrags-Dialog nennt
        sie als Rueckzahlbetrag."""
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_PAID)
        _set(client, draft.id, Invoice.STATUS_CANCELLED)
        page = client.get(f"/invoices/{draft.id}").get_data(as_text=True)
        assert "Storno-Rechnung erstellen" in page
        assert "120,00" in page


# ---------------------------------------------------------------------------
# Versand-Wege: JEDER muss den Rueckzahlungs-Posten korrekt anlegen
# ---------------------------------------------------------------------------

class TestRefundOpenItemOnEverySendPath:
    """Eine Rechnung wird an mehreren Stellen "versendet" — Statuswechsel,
    Bulk-Statuswechsel, Mailversand, Sammeldruck. Alle laufen ueber
    ``create_or_update_open_item``; fuer eine Gutschrift muss dabei ueberall
    der Rueckzahlungs-Posten entstehen und NICHT einer ueber die Belegsumme.
    Hier faellt der Unterschied auf, weil nur 50 von 120 EUR bezahlt wurden.
    """

    @pytest.fixture
    def partially_paid_credit_note(self, client, admin, draft):
        _login(client)
        _issue(client, draft)
        # Teilzahlung ueber 50 EUR (Rechnung lautet ueber 120 EUR).
        client.post(f"/invoices/{draft.id}/pay", data={"amount": "50,00"},
                    follow_redirects=True)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _credit_note_of(draft.id)
        assert cn.total_amount == Decimal("-120.00")
        return cn

    def test_status_change_uses_refund_not_document_total(
            self, client, admin, partially_paid_credit_note):
        cn = partially_paid_credit_note
        _set(client, cn.id, Invoice.STATUS_SENT)
        oi = db.session.get(Invoice, cn.id).open_item
        assert oi is not None
        # 50 zurueckzuzahlen — nicht 120.
        assert oi.amount == Decimal("-50.00")

    def test_bulk_status_change_uses_refund(
            self, client, admin, partially_paid_credit_note):
        cn = partially_paid_credit_note
        client.post("/invoices/bulk-action",
                    data={"action": Invoice.STATUS_SENT, "invoice_ids": str(cn.id)},
                    follow_redirects=True)
        oi = db.session.get(Invoice, cn.id).open_item
        assert oi is not None and oi.amount == Decimal("-50.00")

    def test_email_send_uses_refund(self, client, admin, monkeypatch,
                                    partially_paid_credit_note):
        """Der Weg, den der Nutzer real geht: Gutschrift per Mail verschicken.

        Dokumentformat auf Word gestellt — WeasyPrint braucht GTK und ist
        lokal nicht ladbar; die Route wuerde sonst vor der OP-Anlage
        abbrechen. Der geprueftе Code-Pfad ist derselbe.
        """
        from app.models import AppSetting
        from app.invoices import routes as inv_routes

        AppSetting.set("invoice.document_format", "docx")
        monkeypatch.setattr(inv_routes, "send_mail", lambda msg: None)

        cn = partially_paid_credit_note
        cust = db.session.get(Invoice, cn.id).customer
        cust.email = "kunde@test.local"
        cust.rechnung_per_email = True
        db.session.commit()

        client.post(f"/invoices/{cn.id}/send-email", data={"test_mode": "0"},
                    follow_redirects=True)
        cn = db.session.get(Invoice, cn.id)
        assert cn.status == Invoice.STATUS_SENT
        assert cn.open_item is not None
        assert cn.open_item.amount == Decimal("-50.00")

    def test_partial_payment_stays_booked(self, partially_paid_credit_note):
        original = db.session.get(Invoice, partially_paid_credit_note.cancels_invoice_id)
        assert original.paid_amount == Decimal("50.00")

    def test_draft_pdf_already_announces_the_refund(
            self, app, client, admin, partially_paid_credit_note):
        """Der Entwurfs-Ausdruck muss den Rückzahlbetrag schon nennen.

        Regression: das PDF las den Betrag aus dem Offenen Posten — den gibt
        es beim Entwurf noch nicht, das Dokument behauptete deshalb
        fälschlich „kein Rückzahlungsbetrag". Genau dieses Dokument schickt
        der Nutzer aber raus.
        """
        from app.invoices.routes import _render_pdf_html
        cn = partially_paid_credit_note
        assert cn.status == Invoice.STATUS_DRAFT and cn.open_item is None
        with app.test_request_context():
            html = _render_pdf_html(cn)
        assert "50,00" in html
        assert "kein Rückzahlungsbetrag" not in html


# ---------------------------------------------------------------------------
# Rundlauf Zahlung -> Storno -> Rueckueberweisung: die Buchhaltung muss 0 ergeben
# ---------------------------------------------------------------------------

def _refund_via_bank(credit_note, amount):
    """Rueckueberweisung wie aus dem Bankauszug-Import auf den Posten der Gutschrift."""
    from app.bank_import.services import _book_against_op
    from app.models import BankStatement, BankStatementLine

    ra = RealAccount.query.first()
    stmt = BankStatement(format=BankStatement.FORMAT_CAMT053, filename="r.xml",
                         file_hash=f"h{credit_note.id}", real_account_id=ra.id)
    db.session.add(stmt)
    db.session.flush()
    line = BankStatementLine(amount=Decimal(amount), booking_date=date.today(),
                             purpose=credit_note.invoice_number)
    _book_against_op(credit_note.open_item, Decimal(amount), line, stmt, None)
    db.session.commit()


def _ledger_total():
    """Summe aller Buchungen (Storno-Paare heben sich auf) = Bewegung am Konto."""
    return sum((Decimal(str(b.amount)) for b in Booking.query.all()), Decimal("0"))


class TestRefundRoundTrip:
    """Der Fall aus der Praxis: Kunde hat bezahlt, Rechnung war falsch, sie wird
    mit Gutschrift storniert und das Geld zurueckueberwiesen. Am Bankkonto ist
    netto nichts passiert — in der Buchhaltung darf es auch nicht anders sein.
    """

    def test_payment_storno_refund_nets_to_zero(self, client, admin, draft):
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_PAID)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _issue_credit_note(client, draft.id)
        assert cn.open_item.amount == Decimal("-120.00")

        _refund_via_bank(cn, "-120.00")

        cn = db.session.get(Invoice, cn.id)
        assert cn.open_item.status == OpenItem.STATUS_PAID
        assert cn.status == Invoice.STATUS_PAID
        assert _ledger_total() == Decimal("0")
        # Die Rueckzahlung mindert dasselbe Erloeskonto und dieselbe Steuer.
        refund = Booking.query.filter(Booking.invoice_id == cn.id).one()
        assert refund.account_id == draft._acc_id
        assert refund.tax_rate == Decimal("20")

    def test_cancelling_a_refunded_credit_note_keeps_the_refund(self, client, admin, draft):
        """Auch eine Gutschrift-Stornierung laesst die echte Rueckueberweisung
        stehen — und eine neue Gutschrift zahlt nicht ein zweites Mal zurueck."""
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_PAID)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _issue_credit_note(client, draft.id)
        _refund_via_bank(cn, "-120.00")

        _set(client, cn.id, Invoice.STATUS_CANCELLED)
        assert _ledger_total() == Decimal("0")

        client.post(f"/invoices/{draft.id}/credit-note", data={}, follow_redirects=True)
        second = (Invoice.query.filter_by(cancels_invoice_id=draft.id)
                  .filter(Invoice.id != cn.id).one())
        assert second.refund_amount == Decimal("0")


class TestLegacyReversedPayment:
    """Rechnungen, die vor der Umstellung storniert wurden: dort hat der
    Storno die Zahlung immer mitstorniert."""

    def _legacy_cancel(self, client, draft):
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_PAID)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        inv = db.session.get(Invoice, draft.id)
        # Altes Verhalten nachstellen: Zahlung beim Storno mitstorniert.
        acc_svc.storno_invoice_bookings(inv, reason="alt", created_by_id=None)
        db.session.commit()
        return _credit_note_of(draft.id)

    def test_sent_credit_note_keeps_its_promised_refund(self, client, admin, draft):
        """Ein verschickter Beleg muss beim Neu-Rendern dasselbe sagen."""
        _login(client)
        _issue(client, draft)
        _set(client, draft.id, Invoice.STATUS_PAID)
        _set(client, draft.id, Invoice.STATUS_CANCELLED, create_credit_note="1")
        cn = _issue_credit_note(client, draft.id)
        acc_svc.storno_invoice_bookings(db.session.get(Invoice, draft.id),
                                        reason="alt", created_by_id=None)
        db.session.commit()
        cn = db.session.get(Invoice, cn.id)
        assert cn.refund_amount == Decimal("120.00")
        # Erneutes Versenden aendert den Posten nicht.
        _set(client, cn.id, Invoice.STATUS_SENT)
        assert db.session.get(Invoice, cn.id).open_item.amount == Decimal("-120.00")

    def test_draft_credit_note_has_no_refund(self, client, admin, draft):
        """Zahlung schon ausgebucht -> kein zweiter Rueckzahlungs-Posten."""
        cn = self._legacy_cancel(client, draft)
        assert cn.refund_amount == Decimal("0")
        cn = _issue_credit_note(client, draft.id)
        assert cn.open_item is None

    def test_after_the_fact_dialog_warns(self, client, admin, draft):
        cn = self._legacy_cancel(client, draft)
        client.post(f"/invoices/{cn.id}/delete", follow_redirects=True)
        page = client.get(f"/invoices/{draft.id}").get_data(as_text=True)
        assert "Zeile im\n    Bankauszug-Import überspringen" in page
        assert "nicht</strong> ein zweites Mal" in page
