"""Unit-Tests für app/accounting/services.py — reine Funktionen ohne DB-Abfragen."""
import types
from datetime import date
from decimal import Decimal

from app.accounting.services import (
    _split_invoice_by_dimensions,
    booking_tax,
    is_effective_booking,
    ust_period,
)
from app.models import Booking


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _booking(**kwargs):
    """Mock-Booking ohne DB-Zugriff."""
    defaults = dict(
        status=Booking.STATUS_VERBUCHT,
        storno_of_id=None,
        amount=Decimal("100.00"),
        tax_rate=None,
    )
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _invoice(items, open_item_account=None, billing_run_account=None):
    """Mock-Invoice mit Items für Split-Tests."""
    mock_items = [
        types.SimpleNamespace(
            account_id=it.get("account_id"),
            project_id=it.get("project_id"),
            tax_rate=Decimal(str(it["tax_rate"])) if it.get("tax_rate") is not None else None,
            amount=Decimal(str(it.get("amount", "0"))),
            description=it.get("description", "Testposition"),
        )
        for it in items
    ]
    open_item = types.SimpleNamespace(account_id=open_item_account) if open_item_account else None
    billing_run = types.SimpleNamespace(account_id=billing_run_account) if billing_run_account else None
    return types.SimpleNamespace(items=mock_items, open_item=open_item, billing_run=billing_run)


# ---------------------------------------------------------------------------
# is_effective_booking
# ---------------------------------------------------------------------------

class TestIsEffectiveBooking:
    def test_none_returns_false(self):
        assert is_effective_booking(None) is False

    def test_normal_verbucht_is_effective(self):
        assert is_effective_booking(_booking(status=Booking.STATUS_VERBUCHT)) is True

    def test_offen_is_effective(self):
        assert is_effective_booking(_booking(status=Booking.STATUS_OFFEN)) is True

    def test_storniert_not_effective(self):
        assert is_effective_booking(_booking(status=Booking.STATUS_STORNIERT)) is False

    def test_storno_partner_not_effective(self):
        # storno_of_id gesetzt → Gegenbuchung, zählt nicht
        assert is_effective_booking(_booking(storno_of_id=42)) is False

    def test_storno_partner_even_if_verbucht(self):
        b = _booking(status=Booking.STATUS_VERBUCHT, storno_of_id=99)
        assert is_effective_booking(b) is False


# ---------------------------------------------------------------------------
# booking_tax
# ---------------------------------------------------------------------------

class TestBookingTax:
    def test_no_tax_rate(self):
        b = _booking(amount=Decimal("100.00"), tax_rate=None)
        assert booking_tax(b) == Decimal("0")

    def test_zero_tax_rate(self):
        b = _booking(amount=Decimal("100.00"), tax_rate=0)
        assert booking_tax(b) == Decimal("0")

    def test_10_percent_brutto(self):
        # Brutto 110 → Steuer = 110 * 10 / 110 = 10,00
        b = _booking(amount=Decimal("110.00"), tax_rate=Decimal("10"))
        assert booking_tax(b) == Decimal("10.00")

    def test_20_percent_brutto(self):
        # Brutto 120 → Steuer = 120 * 20 / 120 = 20,00
        b = _booking(amount=Decimal("120.00"), tax_rate=Decimal("20"))
        assert booking_tax(b) == Decimal("20.00")

    def test_negative_amount_gives_positive_tax(self):
        # Ausgaben: negativer Betrag, aber Steuer immer positiv
        b = _booking(amount=Decimal("-110.00"), tax_rate=Decimal("10"))
        assert booking_tax(b) == Decimal("10.00")

    def test_13_percent(self):
        # 130 Brutto @ 13% → Steuer = 130 * 13 / 113 ≈ 14,96
        b = _booking(amount=Decimal("130.00"), tax_rate=Decimal("13"))
        expected = (Decimal("130") * Decimal("13") / Decimal("113")).quantize(Decimal("0.01"))
        assert booking_tax(b) == expected


# ---------------------------------------------------------------------------
# ust_period
# ---------------------------------------------------------------------------

class TestUstPeriod:
    def test_quartal_1(self):
        start, end = ust_period(2024, 1)
        assert start == date(2024, 1, 1)
        assert end == date(2024, 3, 31)

    def test_quartal_2(self):
        start, end = ust_period(2024, 2)
        assert start == date(2024, 4, 1)
        assert end == date(2024, 6, 30)

    def test_quartal_3(self):
        start, end = ust_period(2024, 3)
        assert start == date(2024, 7, 1)
        assert end == date(2024, 9, 30)

    def test_quartal_4(self):
        start, end = ust_period(2024, 4)
        assert start == date(2024, 10, 1)
        assert end == date(2024, 12, 31)

    def test_gesamtjahr(self):
        start, end = ust_period(2024, 0)
        assert start == date(2024, 1, 1)
        assert end == date(2024, 12, 31)

    def test_schaltjahr_quartal_1(self):
        # 2024 ist Schaltjahr – Q1 endet am 31.03.
        start, end = ust_period(2024, 1)
        assert end == date(2024, 3, 31)


# ---------------------------------------------------------------------------
# _split_invoice_by_dimensions
# ---------------------------------------------------------------------------

class TestSplitInvoiceByDimensions:
    def test_single_dimension_one_result(self):
        inv = _invoice([{"account_id": 1, "amount": "100.00"}])
        result = _split_invoice_by_dimensions(inv, Decimal("100.00"))
        assert len(result) == 1
        assert result[0]["amount"] == Decimal("100.00")
        assert result[0]["account_id"] == 1

    def test_two_accounts_two_splits(self):
        inv = _invoice([
            {"account_id": 1, "amount": "50.00", "description": "A"},
            {"account_id": 2, "amount": "50.00", "description": "B"},
        ])
        result = _split_invoice_by_dimensions(inv, Decimal("100.00"))
        assert len(result) == 2
        assert sum(r["amount"] for r in result) == Decimal("100.00")

    def test_same_dimension_merged_into_one(self):
        # Beide Items haben gleiche (account, project, tax_rate) → 1 Split
        inv = _invoice([
            {"account_id": 5, "amount": "30.00"},
            {"account_id": 5, "amount": "70.00"},
        ])
        result = _split_invoice_by_dimensions(inv, Decimal("100.00"))
        assert len(result) == 1
        assert result[0]["amount"] == Decimal("100.00")

    def test_rounding_invariant_three_splits(self):
        # 3 ungleiche Positionen → letzte gleicht Rundungsdifferenz aus
        inv = _invoice([
            {"account_id": 1, "amount": "33.33"},
            {"account_id": 2, "amount": "33.33"},
            {"account_id": 3, "amount": "33.34"},
        ])
        gross = Decimal("100.00")
        result = _split_invoice_by_dimensions(inv, gross)
        assert sum(r["amount"] for r in result) == gross

    def test_rounding_invariant_partial_payment(self):
        # Teilzahlung 60,01 auf 2 Positionen → kein Cent verloren
        inv = _invoice([
            {"account_id": 1, "amount": "60.00"},
            {"account_id": 2, "amount": "40.00"},
        ])
        partial = Decimal("60.01")
        result = _split_invoice_by_dimensions(inv, partial)
        assert sum(r["amount"] for r in result) == partial

    def test_fallback_to_billing_run_account(self):
        # Item ohne account_id → Fallback auf billing_run.account_id
        inv = _invoice(
            [{"account_id": None, "amount": "100.00"}],
            billing_run_account=99,
        )
        result = _split_invoice_by_dimensions(inv, Decimal("100.00"))
        assert result[0]["account_id"] == 99

    def test_fallback_to_open_item_account(self):
        # open_item.account_id hat Vorrang vor billing_run.account_id
        inv = _invoice(
            [{"account_id": None, "amount": "100.00"}],
            open_item_account=77,
            billing_run_account=99,
        )
        result = _split_invoice_by_dimensions(inv, Decimal("100.00"))
        assert result[0]["account_id"] == 77

    def test_different_tax_rates_different_splits(self):
        # Gleicher Account, verschiedene Steuersätze → 2 Splits
        inv = _invoice([
            {"account_id": 1, "tax_rate": "10", "amount": "100.00"},
            {"account_id": 1, "tax_rate": "0", "amount": "100.00"},
        ])
        # Gross-Beträge: 110 + 100 = 210
        gross = Decimal("210.00")
        result = _split_invoice_by_dimensions(inv, gross)
        assert len(result) == 2
        assert sum(r["amount"] for r in result) == gross
