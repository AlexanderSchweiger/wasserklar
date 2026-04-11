"""Integration-Tests für Sammelbuchung (ADR-002): booking_group_from_invoice_payment + Storno."""
from datetime import date
from decimal import Decimal

import pytest

from app.accounting.services import booking_group_from_invoice_payment, storno_booking_group
from app.extensions import db
from app.models import Booking, BookingGroup, Invoice, InvoiceItem


PAYMENT_DATE = date(2024, 1, 20)


def _make_invoice(customer_id, items_data, number="2024-00001"):
    """Legt eine Invoice mit InvoiceItems an und gibt sie zurück."""
    inv = Invoice(
        invoice_number=number,
        customer_id=customer_id,
        status=Invoice.STATUS_SENT,
        date=date(2024, 1, 15),
        total_amount=sum(Decimal(str(it["amount"])) for it in items_data),
    )
    db.session.add(inv)
    db.session.flush()

    for it in items_data:
        db.session.add(InvoiceItem(
            invoice_id=inv.id,
            description=it.get("desc", "Position"),
            quantity=Decimal("1"),
            unit="Stk",
            unit_price=Decimal(str(it["amount"])),
            amount=Decimal(str(it["amount"])),
            tax_rate=it.get("tax_rate"),
            account_id=it.get("account_id"),
            project_id=it.get("project_id"),
        ))
    db.session.commit()
    db.session.refresh(inv)
    return inv


class TestEinzelbuchung:
    """Eine einzige Dimension → einfache Buchung, kein BookingGroup-Header."""

    def test_group_is_none(self, user, account, real_account, customer):
        inv = _make_invoice(customer.id, [
            {"desc": "Wasser", "amount": "100.00", "account_id": account.id},
        ])
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
        )
        assert group is None

    def test_one_booking_created(self, user, account, real_account, customer):
        inv = _make_invoice(customer.id, [
            {"desc": "Wasser", "amount": "100.00", "account_id": account.id},
        ])
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
        )
        assert len(children) == 1
        assert children[0].amount == Decimal("100.00")
        assert children[0].account_id == account.id
        assert children[0].invoice_id == inv.id


class TestSammelbuchung:
    """Zwei oder mehr Dimensionen → BookingGroup-Header mit Kinder-Buchungen."""

    def test_group_header_created(self, user, account, account2, real_account, customer):
        inv = _make_invoice(customer.id, [
            {"desc": "Grundgebühr", "amount": "40.00", "account_id": account.id},
            {"desc": "Verbrauch",   "amount": "60.00", "account_id": account2.id},
        ], number="2024-00002")
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
        )
        assert isinstance(group, BookingGroup)
        assert group.status == BookingGroup.STATUS_AKTIV

    def test_two_children_created(self, user, account, account2, real_account, customer):
        inv = _make_invoice(customer.id, [
            {"desc": "Grundgebühr", "amount": "40.00", "account_id": account.id},
            {"desc": "Verbrauch",   "amount": "60.00", "account_id": account2.id},
        ], number="2024-00003")
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
        )
        assert len(children) == 2

    def test_group_total_equals_payment(self, user, account, account2, real_account, customer):
        payment = Decimal("100.00")
        inv = _make_invoice(customer.id, [
            {"desc": "Pos A", "amount": "40.00", "account_id": account.id},
            {"desc": "Pos B", "amount": "60.00", "account_id": account2.id},
        ], number="2024-00004")
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=payment,
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
        )
        assert group.total_amount == payment

    def test_children_sum_equals_payment(self, user, account, account2, real_account, customer):
        payment = Decimal("100.00")
        inv = _make_invoice(customer.id, [
            {"desc": "Pos A", "amount": "40.00", "account_id": account.id},
            {"desc": "Pos B", "amount": "60.00", "account_id": account2.id},
        ], number="2024-00005")
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=payment,
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
        )
        assert sum(c.amount for c in children) == payment

    def test_rounding_invariant_partial_payment(self, user, account, account2, real_account, customer):
        """Auch bei ungeraden Teilzahlungen darf kein Cent verloren gehen."""
        partial = Decimal("60.01")
        inv = _make_invoice(customer.id, [
            {"desc": "Pos A", "amount": "60.00", "account_id": account.id},
            {"desc": "Pos B", "amount": "40.00", "account_id": account2.id},
        ], number="2024-00006")
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=partial,
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
        )
        assert sum(c.amount for c in children) == partial


class TestSammelbuchungStorno:
    """Storno einer Sammelbuchung muss gruppen-atomar sein (ADR-002)."""

    def _create_group(self, user, account, account2, real_account, customer, number="2024-00010"):
        inv = _make_invoice(customer.id, [
            {"desc": "Pos A", "amount": "50.00", "account_id": account.id},
            {"desc": "Pos B", "amount": "50.00", "account_id": account2.id},
        ], number=number)
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
        )
        db.session.commit()
        return group, children

    def test_group_status_after_storno(self, user, account, account2, real_account, customer):
        group, children = self._create_group(user, account, account2, real_account, customer)
        storno_booking_group(group, reason="Testfehler", created_by_id=user.id)
        assert group.status == BookingGroup.STATUS_STORNIERT

    def test_all_children_storniert(self, user, account, account2, real_account, customer):
        group, children = self._create_group(
            user, account, account2, real_account, customer, number="2024-00011"
        )
        storno_booking_group(group, reason="Testfehler", created_by_id=user.id)
        for child in children:
            assert child.status == Booking.STATUS_STORNIERT

    def test_storno_partner_bookings_created(self, user, account, account2, real_account, customer):
        group, children = self._create_group(
            user, account, account2, real_account, customer, number="2024-00012"
        )
        partners = storno_booking_group(group, reason="Testfehler", created_by_id=user.id)
        db.session.flush()
        assert len(partners) == len(children)
        for partner, child in zip(partners, children):
            assert partner.storno_of_id == child.id
            assert partner.amount == -child.amount

    def test_double_storno_is_noop(self, user, account, account2, real_account, customer):
        group, _ = self._create_group(
            user, account, account2, real_account, customer, number="2024-00013"
        )
        storno_booking_group(group, reason="Erst", created_by_id=user.id)
        db.session.commit()
        partners2 = storno_booking_group(group, reason="Nochmal", created_by_id=user.id)
        assert partners2 == []
