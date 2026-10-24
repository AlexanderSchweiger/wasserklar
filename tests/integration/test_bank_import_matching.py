"""Integration-Tests fuer das IBAN- und Name+Betrag-Matching in bank_import/matching.py.

Deckt die Matching-Pfade ab, die greifen, wenn keine Rechnungsnummer im
Verwendungszweck steht: IBAN des Gegenkontos (``Customer.iban``) + exakter
Betrag, danach reihenfolgeunabhaengiger Namensabgleich + exakter Betrag.
Braucht echte Customer/OpenItem/Invoice-Rows -> Integration-Test.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.bank_import.matching import match_line
from app.models import BankStatementLine, Customer, Invoice, OpenItem


def _customer(name, iban=None, active=True):
    c = Customer(name=name, active=active, iban=iban)
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


def _line(name, amount, purpose=None, iban=None):
    """Transiente Bankzeile (nicht persistiert) — match_line mutiert sie nur."""
    return BankStatementLine(
        counterparty_name=name,
        amount=Decimal(str(amount)),
        purpose=purpose,
        counterparty_iban=iban,
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


IBAN_A = "AT611904300234573201"
IBAN_B = "AT483200000012345864"


class TestIbanMatch:
    def test_iban_exact_amount_without_purpose(self, app):
        # George-OFX: kein Verwendungszweck, Name gekuerzt -> die IBAN greift
        # vor dem Namensabgleich.
        c = _customer("Mustermann-Huber Maximilian", iban=IBAN_A)
        op = _open_item(c, "120.50")
        line = _line("MAXIMILIAN MUSTERMANN-HUBER U. E", "120.50",
                     iban="AT61 1904 3002 3457 3201")
        match_line(line)
        assert line.matched_open_item_id == op.id
        assert line.matched_customer_id == c.id
        assert line.match_type == BankStatementLine.MATCH_IBAN
        assert line.selected is True

    def test_iban_wins_over_unrelated_name(self, app):
        # Kontoinhaber heisst anders als der Kontakt (Konto des Ehepartners).
        c = _customer("Huber Anna", iban=IBAN_A)
        op = _open_item(c, "88.00")
        line = _line("Josef Huber", "88.00", iban=IBAN_A.lower())
        match_line(line)
        assert line.matched_open_item_id == op.id
        assert line.match_type == BankStatementLine.MATCH_IBAN

    def test_invoice_number_beats_iban(self, app):
        a = _customer("Huber Anna", iban=IBAN_A)
        b = _customer("Gruber Karl")
        _open_item(a, "50.00")
        op_b = _open_item(b, "50.00", number="2026-00077")
        line = _line("Anna Huber", "50.00", purpose="Rechnung 2026-00077", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id == op_b.id
        assert line.match_type == BankStatementLine.MATCH_INVOICE_NUMBER

    def test_inactive_customer_matched(self, app):
        # Ehemaliger Kontakt zahlt seine Schlussrechnung.
        c = _customer("Alt Eigentuemer", iban=IBAN_A, active=False)
        op = _open_item(c, "312.40")
        line = _line("ALT EIGENTUEMER", "312.40", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id == op.id
        assert line.match_type == BankStatementLine.MATCH_IBAN

    def test_legacy_iban_with_spaces_matched(self, app):
        # Altbestand, der nicht ueber das Formular normalisiert wurde.
        c = _customer("Huber Anna", iban="at61 1904 3002 3457 3201")
        op = _open_item(c, "64.00")
        line = _line("A. HUBER", "64.00", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id == op.id

    def test_amount_differs_sets_customer_only(self, app):
        c = _customer("Huber Anna", iban=IBAN_A)
        _open_item(c, "50.00")
        line = _line("A. HUBER", "45.00", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id is None
        assert line.matched_customer_id == c.id
        assert line.match_type is None

    def test_iban_customer_beats_name_customer_hint(self, app):
        # Name trifft einen anderen Kontakt ohne passenden Betrag -> die IBAN
        # bestimmt den vorgemerkten Kontakt.
        by_iban = _customer("Huber Anna", iban=IBAN_A)
        _open_item(by_iban, "50.00")
        by_name = _customer("Türk Thomas")
        _open_item(by_name, "60.00")
        line = _line("Thomas Türk", "453.95", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id is None
        assert line.matched_customer_id == by_iban.id

    def test_name_fallback_when_iban_amount_misses(self, app):
        # Zweiter Kontakt derselben Person ohne hinterlegte IBAN: Name + exakter
        # Betrag finden dessen Posten.
        with_iban = _customer("Muster Max", iban=IBAN_A)
        _open_item(with_iban, "50.00")
        without_iban = _customer("Muster Max")
        op = _open_item(without_iban, "75.00")
        line = _line("Max Muster", "75.00", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id == op.id
        assert line.match_type == BankStatementLine.MATCH_NAME


class TestIbanSharedByCustomers:
    """Ehepaar mit gemeinsamem Konto: zwei Kontakte, eine IBAN."""

    def test_unique_amount_assigns(self, app):
        a = _customer("Muster Max", iban=IBAN_A)
        b = _customer("Muster Erika", iban=IBAN_A)
        _open_item(a, "100.00")
        op_b = _open_item(b, "80.00")
        line = _line("MAX U. ERIKA MUSTER", "80.00", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id == op_b.id
        assert line.matched_customer_id == b.id
        assert line.match_type == BankStatementLine.MATCH_IBAN

    def test_same_amount_twice_assigns_nothing(self, app):
        a = _customer("Muster Max", iban=IBAN_A)
        b = _customer("Muster Erika", iban=IBAN_A)
        _open_item(a, "100.00")
        _open_item(b, "100.00")
        line = _line("MAX U. ERIKA MUSTER", "100.00", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id is None
        assert line.matched_customer_id is None

    def test_no_amount_match_two_customers_with_items_no_hint(self, app):
        a = _customer("Muster Max", iban=IBAN_A)
        b = _customer("Muster Erika", iban=IBAN_A)
        _open_item(a, "100.00")
        _open_item(b, "80.00")
        line = _line("M. MUSTER", "180.00", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id is None
        assert line.matched_customer_id is None

    def test_only_one_with_open_items_is_hinted(self, app):
        a = _customer("Muster Max", iban=IBAN_A)
        _customer("Muster Erika", iban=IBAN_A)
        _open_item(a, "100.00")
        line = _line("M. MUSTER", "70.00", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id is None
        assert line.matched_customer_id == a.id


class TestIbanSign:
    def test_outgoing_never_hits_receivable(self, app):
        # Abbuchung an den Kontakt (Gegenkonto = seine IBAN) darf keine offene
        # Forderung treffen.
        c = _customer("Huber Anna", iban=IBAN_A)
        _open_item(c, "120.00")
        line = _line("Huber Anna", "-120.00", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id is None
        assert line.match_type is None

    def test_incoming_never_hits_refund_item(self, app):
        c = _customer("Huber Anna", iban=IBAN_A)
        _open_item(c, "-120.00")
        line = _line("Huber Anna", "120.00", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id is None

    def test_refund_to_customer_hits_credit_item(self, app):
        c = _customer("Huber Anna", iban=IBAN_A)
        _open_item(c, "75.00")
        refund = _open_item(c, "-120.00")
        line = _line("ANNA H.", "-120.00", iban=IBAN_A)
        match_line(line)
        assert line.matched_open_item_id == refund.id
        assert line.match_type == BankStatementLine.MATCH_IBAN


class TestIbanNoMatch:
    def test_unknown_iban_no_match(self, app):
        c = _customer("Huber Anna", iban=IBAN_A)
        _open_item(c, "50.00")
        line = _line("Unbekannt Jemand", "50.00", iban=IBAN_B)
        match_line(line)
        assert line.matched_open_item_id is None
        assert line.matched_customer_id is None

    def test_invalid_iban_ignored(self, app):
        # OFX-ACCTID mit nationaler Kontonummer statt IBAN.
        c = _customer("Huber Anna", iban=IBAN_A)
        _open_item(c, "50.00")
        line = _line("Unbekannt Jemand", "50.00", iban="00234573201")
        match_line(line)
        assert line.matched_open_item_id is None
        assert line.matched_customer_id is None

    def test_missing_iban_falls_back_to_name(self, app):
        c = _customer("Petutschnig Thomas", iban=IBAN_A)
        op = _open_item(c, "168.78")
        line = _line("Thomas Petutschnig", "168.78")
        match_line(line)
        assert line.matched_open_item_id == op.id
        assert line.match_type == BankStatementLine.MATCH_NAME
