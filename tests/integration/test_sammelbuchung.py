"""Integration-Tests für Sammelbuchung (ADR-002): booking_group_from_invoice_payment + Storno.

Die Sammelbuchung splittet die Zahlung einer Rechnung nach den Dimensionen
``(account_id, project_id, tax_rate)`` ihrer Positionen. Das Konto kommt primär
von der Position selbst (``InvoiceItem.account_id``, gesetzt vom Tarif im
Rechnungslauf oder von Hand im Positions-Editor); Positionen ohne eigenes Konto
erben ``fallback_account_id`` bzw. ``OpenItem.account_id``. Liefert der Split
genau eine Zeile, entsteht eine flache Einzelbuchung ohne ``BookingGroup``-
Header; bei >= 2 Zeilen ein Gruppen-Header plus je Split-Zeile ein Kind.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.accounting.services import booking_group_from_invoice_payment, storno_booking_group
from app.extensions import db
from app.models import Booking, BookingGroup, Invoice, InvoiceItem, Project


PAYMENT_DATE = date(2024, 1, 20)


@pytest.fixture
def project(app):
    p = Project(name="Projekt Süd", code="SUD")
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def project2(app):
    p = Project(name="Projekt Nord", code="NRD")
    db.session.add(p)
    db.session.commit()
    return p


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
            {"desc": "Wasser", "amount": "100.00"},
        ])
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
            fallback_account_id=account.id,
        )
        assert group is None

    def test_one_booking_created(self, user, account, real_account, customer):
        inv = _make_invoice(customer.id, [
            {"desc": "Wasser", "amount": "100.00"},
        ])
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
            fallback_account_id=account.id,
        )
        assert len(children) == 1
        assert children[0].amount == Decimal("100.00")
        assert children[0].account_id == account.id
        assert children[0].invoice_id == inv.id


class TestSammelbuchung:
    """Zwei oder mehr Dimensionen → BookingGroup-Header mit Kinder-Buchungen."""

    def test_group_header_created(self, user, account, project, project2, real_account, customer):
        inv = _make_invoice(customer.id, [
            {"desc": "Grundgebühr", "amount": "40.00", "project_id": project.id},
            {"desc": "Verbrauch",   "amount": "60.00", "project_id": project2.id},
        ], number="2024-00002")
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
            fallback_account_id=account.id,
        )
        assert isinstance(group, BookingGroup)
        assert group.status == BookingGroup.STATUS_AKTIV

    def test_two_children_created(self, user, account, project, project2, real_account, customer):
        inv = _make_invoice(customer.id, [
            {"desc": "Grundgebühr", "amount": "40.00", "project_id": project.id},
            {"desc": "Verbrauch",   "amount": "60.00", "project_id": project2.id},
        ], number="2024-00003")
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
            fallback_account_id=account.id,
        )
        assert len(children) == 2
        # Einheitliches Buchungskonto über alle Kinder, getrennt nach Projekt.
        assert all(c.account_id == account.id for c in children)
        assert {c.project_id for c in children} == {project.id, project2.id}

    def test_group_total_equals_payment(self, user, account, project, project2, real_account, customer):
        payment = Decimal("100.00")
        inv = _make_invoice(customer.id, [
            {"desc": "Pos A", "amount": "40.00", "project_id": project.id},
            {"desc": "Pos B", "amount": "60.00", "project_id": project2.id},
        ], number="2024-00004")
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=payment,
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
            fallback_account_id=account.id,
        )
        assert group.total_amount == payment

    def test_children_sum_equals_payment(self, user, account, project, project2, real_account, customer):
        payment = Decimal("100.00")
        inv = _make_invoice(customer.id, [
            {"desc": "Pos A", "amount": "40.00", "project_id": project.id},
            {"desc": "Pos B", "amount": "60.00", "project_id": project2.id},
        ], number="2024-00005")
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=payment,
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
            fallback_account_id=account.id,
        )
        assert sum(c.amount for c in children) == payment

    def test_rounding_invariant_partial_payment(self, user, account, project, project2, real_account, customer):
        """Auch bei ungeraden Teilzahlungen darf kein Cent verloren gehen."""
        partial = Decimal("60.01")
        inv = _make_invoice(customer.id, [
            {"desc": "Pos A", "amount": "60.00", "project_id": project.id},
            {"desc": "Pos B", "amount": "40.00", "project_id": project2.id},
        ], number="2024-00006")
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=partial,
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
            fallback_account_id=account.id,
        )
        assert sum(c.amount for c in children) == partial


class TestSammelbuchungStorno:
    """Storno einer Sammelbuchung muss gruppen-atomar sein (ADR-002)."""

    def _create_group(self, user, account, project, project2, real_account, customer, number="2024-00010"):
        inv = _make_invoice(customer.id, [
            {"desc": "Pos A", "amount": "50.00", "project_id": project.id},
            {"desc": "Pos B", "amount": "50.00", "project_id": project2.id},
        ], number=number)
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
            fallback_account_id=account.id,
        )
        db.session.commit()
        return group, children

    def test_group_status_after_storno(self, user, account, project, project2, real_account, customer):
        group, children = self._create_group(user, account, project, project2, real_account, customer)
        storno_booking_group(group, reason="Testfehler", created_by_id=user.id)
        assert group.status == BookingGroup.STATUS_STORNIERT

    def test_all_children_storniert(self, user, account, project, project2, real_account, customer):
        group, children = self._create_group(
            user, account, project, project2, real_account, customer, number="2024-00011"
        )
        storno_booking_group(group, reason="Testfehler", created_by_id=user.id)
        for child in children:
            assert child.status == Booking.STATUS_STORNIERT

    def test_storno_partner_bookings_created(self, user, account, project, project2, real_account, customer):
        group, children = self._create_group(
            user, account, project, project2, real_account, customer, number="2024-00012"
        )
        partners = storno_booking_group(group, reason="Testfehler", created_by_id=user.id)
        db.session.flush()
        assert len(partners) == len(children)
        for partner, child in zip(partners, children):
            assert partner.storno_of_id == child.id
            assert partner.amount == -child.amount

    def test_double_storno_is_noop(self, user, account, project, project2, real_account, customer):
        group, _ = self._create_group(
            user, account, project, project2, real_account, customer, number="2024-00013"
        )
        storno_booking_group(group, reason="Erst", created_by_id=user.id)
        db.session.commit()
        partners2 = storno_booking_group(group, reason="Nochmal", created_by_id=user.id)
        assert partners2 == []


class TestKontoDimension:
    """Konto als dritte Split-Dimension (v1.43.0).

    Deckt die Kernanforderung ab: unterschiedliche Konten erzeugen eine
    Sammelbuchung, ein einheitliches Konto (bei gleichem Projekt und
    Steuersatz) dagegen eine ganz normale Einzelbuchung.
    """

    def test_two_accounts_create_group(self, user, account, account2, real_account, customer):
        inv = _make_invoice(customer.id, [
            {"desc": "Wasserverbrauch", "amount": "80.00", "account_id": account.id},
            {"desc": "Grundgebühr", "amount": "20.00", "account_id": account2.id},
        ])
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
        )
        db.session.commit()
        assert group is not None
        assert len(children) == 2
        assert {c.account_id for c in children} == {account.id, account2.id}
        assert sum(c.amount for c in children) == Decimal("100.00")

    def test_same_account_and_project_stays_single_booking(
            self, user, account, project, real_account, customer):
        """Alles auf dasselbe Konto + Projekt → normale Buchung, keine Gruppe."""
        inv = _make_invoice(customer.id, [
            {"desc": "Wasserverbrauch", "amount": "80.00",
             "account_id": account.id, "project_id": project.id},
            {"desc": "Grundgebühr", "amount": "20.00",
             "account_id": account.id, "project_id": project.id},
        ])
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
        )
        db.session.commit()
        assert group is None
        assert len(children) == 1
        assert children[0].account_id == account.id
        assert children[0].project_id == project.id
        assert children[0].amount == Decimal("100.00")
        assert BookingGroup.query.count() == 0

    def test_item_account_wins_over_fallback(self, user, account, account2,
                                             real_account, customer):
        """Kontierte Position ignoriert den Fallback, unkontierte erbt ihn."""
        inv = _make_invoice(customer.id, [
            {"desc": "Wasserverbrauch", "amount": "80.00", "account_id": account.id},
            {"desc": "Sonstiges", "amount": "20.00"},
        ])
        group, children = booking_group_from_invoice_payment(
            invoice=inv, amount=Decimal("100.00"),
            payment_date=PAYMENT_DATE,
            real_account_id=real_account.id,
            created_by_id=user.id,
            fallback_account_id=account2.id,
        )
        db.session.commit()
        assert group is not None
        by_account = {c.account_id: c.amount for c in children}
        assert by_account == {account.id: Decimal("80.00"), account2.id: Decimal("20.00")}

    def test_without_any_account_raises(self, user, real_account, customer):
        """Ohne Kontierung und ohne Fallback bleibt die Buchung unmöglich."""
        inv = _make_invoice(customer.id, [{"desc": "Wasser", "amount": "100.00"}])
        with pytest.raises(ValueError):
            booking_group_from_invoice_payment(
                invoice=inv, amount=Decimal("100.00"),
                payment_date=PAYMENT_DATE,
                real_account_id=real_account.id,
                created_by_id=user.id,
            )


class TestInvoiceMissingAccount:
    """``invoice_missing_account`` entscheidet, ob der Bezahlt-Dialog nötig ist."""

    def test_true_without_any_account(self, app, customer):
        from app.accounting.services import invoice_missing_account
        inv = _make_invoice(customer.id, [{"desc": "Wasser", "amount": "100.00"}])
        assert invoice_missing_account(inv) is True

    def test_false_when_all_items_contexted(self, app, account, customer):
        from app.accounting.services import invoice_missing_account
        inv = _make_invoice(customer.id, [
            {"desc": "Wasser", "amount": "80.00", "account_id": account.id},
            {"desc": "Grundgebühr", "amount": "20.00", "account_id": account.id},
        ])
        assert invoice_missing_account(inv) is False

    def test_true_when_one_item_uncontexted(self, app, account, customer):
        from app.accounting.services import invoice_missing_account
        inv = _make_invoice(customer.id, [
            {"desc": "Wasser", "amount": "80.00", "account_id": account.id},
            {"desc": "Sonstiges", "amount": "20.00"},
        ])
        assert invoice_missing_account(inv) is True

    def test_false_with_explicit_fallback(self, app, account, customer):
        from app.accounting.services import invoice_missing_account
        inv = _make_invoice(customer.id, [{"desc": "Wasser", "amount": "100.00"}])
        assert invoice_missing_account(inv, fallback_account_id=account.id) is False
