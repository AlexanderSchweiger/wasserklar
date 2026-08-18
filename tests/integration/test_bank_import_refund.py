"""Integration-Tests fuer die Rueckueberweisung im Bankauszug-Import.

Gegenstueck zum Zahlungseingang: wird eine Storno-Rechnung (Gutschrift)
ausgestellt und der Betrag an den Kunden zurueckueberwiesen, erscheint die
Abbuchung als **negative** Bankzeile. Sie muss auf den negativen Offenen Posten
der Gutschrift laufen — und niemals auf eine offene Forderung (das waere eine
Ausgabe, die faelschlich als Zahlungseingang verbucht wuerde).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.bank_import.matching import match_line
from app.bank_import.services import _book_against_op
from app.extensions import db
from app.models import (
    Account, BankStatement, BankStatementLine, Customer, Invoice, OpenItem,
    RealAccount,
)


def _customer(name="Huber Anna"):
    c = Customer(name=name, active=True)
    db.session.add(c)
    db.session.commit()
    return c


def _invoice_with_op(customer, number, amount, *, kind=Invoice.KIND_STANDARD,
                     cancels=None, account_id=None):
    inv = Invoice(
        invoice_number=number, customer_id=customer.id,
        status=Invoice.STATUS_SENT, date=date(2026, 1, 1),
        total_amount=Decimal(str(amount)), invoice_kind=kind,
        cancels_invoice_id=cancels.id if cancels is not None else None,
    )
    db.session.add(inv)
    db.session.flush()
    op = OpenItem(
        customer_id=customer.id, description=number,
        amount=Decimal(str(amount)), status=OpenItem.STATUS_OPEN,
        invoice_id=inv.id, account_id=account_id,
    )
    db.session.add(op)
    db.session.commit()
    return inv, op


def _line(amount, purpose=None, name=None):
    return BankStatementLine(
        counterparty_name=name, amount=Decimal(str(amount)), purpose=purpose,
        booking_date=date(2026, 2, 1),
    )


class TestRefundMatching:
    def test_credit_note_number_in_purpose(self, app):
        c = _customer()
        orig, _ = _invoice_with_op(c, "2026-00010", "120.00")
        credit, cop = _invoice_with_op(
            c, "2026-00011", "-120.00", kind=Invoice.KIND_CREDIT_NOTE, cancels=orig)

        line = _line("-120.00", purpose="Gutschrift 2026-00011")
        match_line(line)
        assert line.matched_open_item_id == cop.id
        assert line.matched_invoice_id == credit.id
        assert line.match_type == BankStatementLine.MATCH_INVOICE_NUMBER

    def test_original_number_in_purpose_finds_credit_note(self, app):
        """Der Kassier tippt beim Rueckueberweisen gern die Originalnummer."""
        c = _customer()
        orig, oop = _invoice_with_op(c, "2026-00010", "120.00")
        orig.status = Invoice.STATUS_CANCELLED
        oop.status = OpenItem.STATUS_PAID
        credit, cop = _invoice_with_op(
            c, "2026-00011", "-120.00", kind=Invoice.KIND_CREDIT_NOTE, cancels=orig)
        db.session.commit()

        line = _line("-120.00", purpose="Storno 2026-00010")
        match_line(line)
        assert line.matched_open_item_id == cop.id

    def test_refund_never_hits_an_open_receivable(self, app):
        """Negative Zeile + Nummer einer offenen Forderung -> kein Treffer."""
        c = _customer()
        _invoice_with_op(c, "2026-00010", "120.00")
        line = _line("-120.00", purpose="2026-00010")
        match_line(line)
        assert line.matched_open_item_id is None

    def test_payment_never_hits_a_credit_note(self, app):
        """Positive Zeile + Gutschriftsnummer -> kein Treffer."""
        c = _customer()
        orig, _ = _invoice_with_op(c, "2026-00010", "120.00")
        _invoice_with_op(c, "2026-00011", "-120.00",
                         kind=Invoice.KIND_CREDIT_NOTE, cancels=orig)
        line = _line("120.00", purpose="2026-00011")
        match_line(line)
        # Trifft nicht die Gutschrift; ohne Namen bleibt die Zeile offen.
        assert line.matched_open_item_id is None

    def test_name_and_negative_amount(self, app):
        """Ohne Verwendungszweck greift Name + exakter (negativer) Betrag."""
        c = _customer("Petutschnig Thomas")
        orig, oop = _invoice_with_op(c, "2026-00010", "168.78")
        oop.status = OpenItem.STATUS_PAID
        _, cop = _invoice_with_op(c, "2026-00011", "-168.78",
                                  kind=Invoice.KIND_CREDIT_NOTE, cancels=orig)
        db.session.commit()

        line = _line("-168.78", name="Thomas Petutschnig")
        match_line(line)
        assert line.matched_open_item_id == cop.id
        assert line.match_type == BankStatementLine.MATCH_NAME


class TestRefundBooking:
    def _fixtures(self, app):
        acc = Account(name="Wasser", code="W01")
        ra = RealAccount(name="Bank", iban="AT00", active=True)
        db.session.add_all([acc, ra])
        db.session.commit()
        stmt = BankStatement(
            format=BankStatement.FORMAT_CAMT053, filename="x.xml",
            file_hash="h", real_account_id=ra.id)
        db.session.add(stmt)
        db.session.commit()
        return acc, stmt

    def test_full_refund_closes_op_and_marks_credit_note_paid(self, app):
        acc, stmt = self._fixtures(app)
        c = _customer()
        orig, _ = _invoice_with_op(c, "2026-00010", "120.00", account_id=acc.id)
        credit, cop = _invoice_with_op(
            c, "2026-00011", "-120.00", kind=Invoice.KIND_CREDIT_NOTE,
            cancels=orig, account_id=acc.id)

        line = _line("-120.00")
        _book_against_op(cop, Decimal("-120.00"), line, stmt, None)
        db.session.commit()

        assert cop.open_balance == Decimal("0")
        assert cop.status == OpenItem.STATUS_PAID
        assert db.session.get(Invoice, credit.id).status == Invoice.STATUS_PAID

    def test_partial_refund_stays_open(self, app):
        """Halbe Rueckzahlung: der Posten ist teilbezahlt, nicht 'Gutschrift'."""
        acc, stmt = self._fixtures(app)
        c = _customer()
        orig, _ = _invoice_with_op(c, "2026-00010", "120.00", account_id=acc.id)
        credit, cop = _invoice_with_op(
            c, "2026-00011", "-120.00", kind=Invoice.KIND_CREDIT_NOTE,
            cancels=orig, account_id=acc.id)

        line = _line("-50.00")
        _book_against_op(cop, Decimal("-50.00"), line, stmt, None)
        db.session.commit()

        assert cop.open_balance == Decimal("-70.00")
        assert cop.status == OpenItem.STATUS_PARTIAL
        assert db.session.get(Invoice, credit.id).status == Invoice.STATUS_SENT
