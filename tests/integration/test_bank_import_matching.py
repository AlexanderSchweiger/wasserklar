"""Integration-Tests fuer das Name+Betrag-Matching in bank_import/matching.py.

Deckt den zweiten Matching-Pfad ab (greift, wenn keine Rechnungsnummer im
Verwendungszweck steht): reihenfolgeunabhaengiger Namensabgleich + exakter
Betrag. Braucht echte Customer/OpenItem/Invoice-Rows -> Integration-Test.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.bank_import.matching import match_line
from app.models import BankStatementLine, Customer, Invoice, OpenItem


def _customer(name):
    c = Customer(name=name, active=True)
    db.session.add(c)
    db.session.commit()
    return c


def _open_item(customer, amount, number=None):
    """Offener Posten ohne Buchungen -> open_balance == amount."""
    invoice_id = None
    if number:
        inv = Invoice(
            invoice_number=number,
            customer_id=customer.id,
            status=Invoice.STATUS_SENT,
            date=date(2026, 1, 1),
            total_amount=Decimal(str(amount)),
        )
        db.session.add(inv)
        db.session.flush()
        invoice_id = inv.id
    op = OpenItem(
        customer_id=customer.id,
        description="Wasserrechnung",
        amount=Decimal(str(amount)),
        status=OpenItem.STATUS_OPEN,
        invoice_id=invoice_id,
    )
    db.session.add(op)
    db.session.commit()
    return op


def _line(name, amount, purpose=None):
    """Transiente Bankzeile (nicht persistiert) — match_line mutiert sie nur."""
    return BankStatementLine(
        counterparty_name=name,
        amount=Decimal(str(amount)),
        purpose=purpose,
    )


class TestNameAmountMatch:
    def test_reversed_name_exact_amount(self, app):
        c = _customer("Petutschnig Thomas")
        op = _open_item(c, "168.78")
        line = _line("Thomas Petutschnig", "168.78")
        match_line(line)
        assert line.matched_open_item_id == op.id
        assert line.matched_customer_id == c.id
        assert line.match_type == BankStatementLine.MATCH_NAME

    def test_title_stripped_and_reversed(self, app):
        c = _customer("Gorgasser Iris")
        op = _open_item(c, "351.38")
        line = _line("Dr. Iris Gorgasser", "351.38")
        match_line(line)
        assert line.matched_open_item_id == op.id

    def test_umlaut_name_matched(self, app):
        # ILIKE-Vorfilter muss den akzentbehafteten DB-Namen treffen.
        c = _customer("Schönlieb Thomas")
        op = _open_item(c, "371.39")
        line = _line("Dr. Thomas Schönlieb", "371.39")
        match_line(line)
        assert line.matched_open_item_id == op.id

    def test_same_surname_different_person_not_matched(self, app):
        # Zwei Petutschnigs, nur Thomas hat den passenden Betrag.
        thomas = _customer("Petutschnig Thomas")
        rudolf = _customer("Petutschnig Rudolf")
        op_t = _open_item(thomas, "168.78")
        _open_item(rudolf, "300.10")
        line = _line("Thomas Petutschnig", "168.78")
        match_line(line)
        assert line.matched_open_item_id == op_t.id
        assert line.matched_customer_id == thomas.id

    def test_name_matches_but_amount_differs_sets_customer_only(self, app):
        c = _customer("Türk Thomas")
        _open_item(c, "50.00")
        line = _line("Thomas Türk", "453.95")
        match_line(line)
        assert line.matched_open_item_id is None
        assert line.matched_customer_id == c.id  # Kunde erkannt, OP-Wahl manuell

    def test_ambiguous_amount_across_two_customers_no_match(self, app):
        # Zwei gleichnamige Kunden mit demselben offenen Betrag -> nicht eindeutig.
        a = _customer("Müller Thomas")
        b = _customer("Müller Thomas")
        _open_item(a, "100.00")
        _open_item(b, "100.00")
        line = _line("Thomas Müller", "100.00")
        match_line(line)
        assert line.matched_open_item_id is None
        assert line.matched_customer_id is None

    def test_invoice_number_takes_priority(self, app):
        c = _customer("Petutschnig Thomas")
        op = _open_item(c, "168.78", number="2026-00038")
        # Auch wenn der Name passt: die Rechnungsnummer im Zweck gewinnt.
        line = _line("Irgendwer Anders", "999.99", purpose="Zahlung 2026-00038")
        match_line(line)
        assert line.matched_open_item_id == op.id
        assert line.match_type == BankStatementLine.MATCH_INVOICE_NUMBER

    def test_unknown_name_no_match(self, app):
        _customer("Bekannt Kunde")
        line = _line("Völlig Unbekannt", "123.45")
        match_line(line)
        assert line.matched_open_item_id is None
        assert line.matched_customer_id is None
