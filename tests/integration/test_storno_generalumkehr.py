"""Storno als Generalumkehr: Gegenbuchung am Storno-Datum, Auswertungen zählen beide Hälften.

Fachlicher Kern: ein Storno ändert nie einen schon vergangenen Zeitraum. Das
Original bleibt in seinem Monat/Quartal/Jahr stehen, die Umkehr steht im
Zeitraum des Stornos — negativ auf der Seite des Originals (eine stornierte
Einnahme mindert die Einnahmen, sie ist keine Ausgabe).
"""
from datetime import date
from decimal import Decimal

from app.accounting import services as svc
from app.extensions import db
from app.models import Account, Booking, BookingGroup, Customer, FiscalYear, RealAccount


def _setup():
    acc = Account(name="Wassergebühren", code="WG1")
    exp = Account(name="Material", code="MA1")
    ra = RealAccount(name="Bank", opening_balance=Decimal("1000.00"))
    cust = Customer(name="Huber Hans", customer_number=7)
    db.session.add_all([acc, exp, ra, cust])
    db.session.commit()
    return acc, exp, ra, cust


def _booking(acc, ra, amount, d, cust=None, tax_rate=None, reference=None):
    b = Booking(date=d, account_id=acc.id, real_account_id=ra.id, amount=Decimal(amount),
                description="Wasser 2026", status=Booking.STATUS_VERBUCHT,
                customer_id=cust.id if cust else None, tax_rate=tax_rate, reference=reference)
    db.session.add(b)
    db.session.commit()
    return b


class TestStornoBooking:
    def test_partner_carries_storno_date_and_all_fields(self, app):
        acc, _exp, ra, cust = _setup()
        b = _booking(acc, ra, "120.00", date(2026, 2, 10), cust=cust,
                     tax_rate=Decimal("10"), reference="RE-1")
        partner = svc.storno_booking(b, "falsch erfasst", None, storno_date=date(2026, 8, 3))
        db.session.commit()

        assert partner.date == date(2026, 8, 3)
        assert partner.amount == Decimal("-120.00")
        assert partner.real_account_id == ra.id
        assert partner.customer_id == cust.id
        assert partner.reference == "RE-1"
        assert partner.tax_rate == Decimal("10")
        assert partner.storno_of_id == b.id
        assert b.status == Booking.STATUS_STORNIERT
        assert b.date == date(2026, 2, 10)                 # Original bleibt unverändert
        assert b.storno_date == date(2026, 8, 3)
        assert b.storno_reason == "falsch erfasst"

    def test_group_partners_carry_storno_date(self, app):
        acc, exp, ra, _cust = _setup()
        group = BookingGroup(date=date(2026, 1, 15), description="Sammel", total_amount=Decimal("0"))
        db.session.add(group)
        db.session.flush()
        for a, amount in ((acc, "50.00"), (exp, "30.00")):
            db.session.add(Booking(date=group.date, account_id=a.id, real_account_id=ra.id,
                                   amount=Decimal(amount), description="Zeile",
                                   status=Booking.STATUS_VERBUCHT, group_id=group.id))
        db.session.commit()

        partners = svc.storno_booking_group(group, "doppelt", None, storno_date=date(2026, 7, 1))
        db.session.commit()
        assert {p.date for p in partners} == {date(2026, 7, 1)}
        assert all(p.group_id == group.id for p in partners)


class TestLedgerCountsBothHalves:
    def test_ust_original_quarter_stays_storno_quarter_negative(self, app):
        acc, _exp, ra, _cust = _setup()
        b = _booking(acc, ra, "110.00", date(2026, 2, 10), tax_rate=Decimal("10"))
        svc.storno_booking(b, "Fehler", None, storno_date=date(2026, 8, 3))
        db.session.commit()

        q1 = svc.ust_totals(2026, 1)
        q3 = svc.ust_totals(2026, 3)
        year = svc.ust_totals(2026, 0)
        assert q1["total_ust"] == Decimal("10.00")             # gemeldetes Q1 bleibt
        assert q3["total_ust"] == Decimal("-10.00")            # Umkehr im Q3
        assert q3["total_vst"] == Decimal("0")                 # keine Vorsteuer!
        assert year["total_ust"] == Decimal("0.00")
        assert year["ust_brutto"] == Decimal("0.00")

    def test_storno_of_an_expense_reduces_input_tax(self, app):
        _acc, exp, ra, _cust = _setup()
        b = _booking(exp, ra, "-120.00", date(2026, 3, 1), tax_rate=Decimal("20"))
        svc.storno_booking(b, "Gutschrift Lieferant", None, storno_date=date(2026, 5, 2))
        db.session.commit()

        assert svc.ust_totals(2026, 1)["total_vst"] == Decimal("20.00")
        q2 = svc.ust_totals(2026, 2)
        assert q2["total_vst"] == Decimal("-20.00") and q2["total_ust"] == Decimal("0")

    def test_income_storno_reduces_income_not_expense(self, app):
        acc, _exp, ra, _cust = _setup()
        _booking(acc, ra, "300.00", date(2026, 1, 5))
        b = _booking(acc, ra, "100.00", date(2026, 2, 1))
        svc.storno_booking(b, "Fehler", None, storno_date=date(2026, 6, 1))
        db.session.commit()

        inc_rows, exp_rows, total_inc, total_exp, balance = svc.year_income_expense(2026)
        assert total_inc == Decimal("300.00")
        assert total_exp == Decimal("0") and exp_rows == []
        assert balance == Decimal("300.00")

    def test_storno_across_years_keeps_both_years(self, app):
        """Original 2025 (abgeschlossen), Storno 2026: 2025 bleibt, 2026 trägt die Umkehr."""
        acc, _exp, ra, _cust = _setup()
        b = _booking(acc, ra, "200.00", date(2025, 11, 3))
        db.session.add(FiscalYear(year=2025, start_date=date(2025, 1, 1),
                                  end_date=date(2025, 12, 31), closed=True))
        db.session.commit()
        assert svc.storno_blocker(date(2026, 3, 1)) is None    # Original-Jahr zu → egal

        svc.storno_booking(b, "Fehler", None, storno_date=date(2026, 3, 1))
        db.session.commit()

        assert svc.year_income_expense(2025)[2] == Decimal("200.00")
        inc_rows, _e, total_inc_2026, _te, _bal = svc.year_income_expense(2026)
        assert total_inc_2026 == Decimal("-200.00")
        assert inc_rows == [(acc.id, acc.name, Decimal("-200.00"))]
        assert svc.year_end_balance(ra, 2025) == Decimal("1200.00")
        assert svc.year_end_balance(ra, 2026) == Decimal("1000.00")
        income, expense, total = svc.year_movements(ra.id, 2026)
        assert (income, expense, total) == (Decimal("-200.00"), Decimal("0"), Decimal("-200.00"))

    def test_bank_balance_moves_back_with_partner(self, app):
        acc, _exp, ra, _cust = _setup()
        b = _booking(acc, ra, "80.00", date(2026, 4, 1))
        svc.storno_booking(b, "Fehler", None, storno_date=date(2026, 4, 2))
        db.session.commit()
        assert svc.current_balance(ra) == Decimal("1000.00")

    def test_orphan_storniert_without_partner_stays_out(self, app):
        """Altbestand: auf „Storniert“ gesetzt ohne Gegenbuchung — zählte nie, zählt nicht."""
        acc, _exp, ra, _cust = _setup()
        b = _booking(acc, ra, "55.00", date(2026, 4, 1))
        b.status = Booking.STATUS_STORNIERT
        db.session.commit()
        assert svc.year_income_expense(2026)[2] == Decimal("0")
        assert svc.current_balance(ra) == Decimal("1000.00")
        assert svc.counts_in_ledger(b) is False


class TestStornoBlocker:
    def test_closed_storno_year_blocks(self, app):
        db.session.add(FiscalYear(year=2026, start_date=date(2026, 1, 1),
                                  end_date=date(2026, 12, 31), closed=True))
        db.session.commit()
        assert "abgeschlossen" in svc.storno_blocker(date(2026, 5, 1))

    def test_invoice_storno_allowed_for_closed_original_year(self, app):
        """Rechnungs-Storno-Kaskade: entscheidend ist das Jahr des Stornos."""
        from app.models import BillingPeriod, Invoice
        acc, _exp, ra, cust = _setup()
        period = BillingPeriod(name="2025", start_date=date(2025, 1, 1),
                               end_date=date(2025, 12, 31), active=True)
        db.session.add(period)
        db.session.add(FiscalYear(year=2025, start_date=date(2025, 1, 1),
                                  end_date=date(2025, 12, 31), closed=True))
        db.session.flush()
        inv = Invoice(invoice_number="2025-00001", customer_id=cust.id, billing_period_id=period.id,
                      date=date(2025, 3, 1), due_date=date(2025, 3, 31), status=Invoice.STATUS_PAID)
        db.session.add(inv)
        db.session.flush()
        b = _booking(acc, ra, "99.00", date(2025, 3, 20), cust=cust)
        b.invoice_id = inv.id
        db.session.commit()

        assert svc.storno_invoice_bookings(inv, "Storno", None, storno_date=date(2026, 2, 1)) is None
        db.session.commit()
        partner = b.storno_buchung
        assert partner.date == date(2026, 2, 1) and partner.real_account_id == ra.id
