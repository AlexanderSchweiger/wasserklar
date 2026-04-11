"""Integration-Tests für den Storno-Filter und Kontostand-Berechnungen mit echten DB-Objekten."""
from datetime import date
from decimal import Decimal

from app.accounting.services import (
    apply_storno_filter,
    is_effective_booking,
    jan1_balance,
    year_end_balance,
)
from app.extensions import db
from app.models import Account, Booking, RealAccount


def _setup(app):
    acc = Account(name="Einnahmen", code="EIN")
    ra = RealAccount(name="Bank", opening_balance=Decimal("0"))
    db.session.add_all([acc, ra])
    db.session.commit()
    return acc, ra


def _booking(acc_id, ra_id, amount, status=Booking.STATUS_VERBUCHT, storno_of_id=None, d=None):
    b = Booking(
        date=d or date(2024, 6, 1),
        account_id=acc_id,
        amount=amount,
        description="Test",
        real_account_id=ra_id,
        status=status,
        storno_of_id=storno_of_id,
    )
    db.session.add(b)
    db.session.flush()
    return b


class TestIsEffectiveBookingWithDB:
    def test_normal_booking_effective(self, app):
        acc, ra = _setup(app)
        b = _booking(acc.id, ra.id, Decimal("100.00"))
        assert is_effective_booking(b) is True

    def test_storniert_not_effective(self, app):
        acc, ra = _setup(app)
        b = _booking(acc.id, ra.id, Decimal("100.00"), status=Booking.STATUS_STORNIERT)
        assert is_effective_booking(b) is False

    def test_storno_partner_not_effective(self, app):
        acc, ra = _setup(app)
        original = _booking(acc.id, ra.id, Decimal("100.00"), status=Booking.STATUS_STORNIERT)
        partner = _booking(acc.id, ra.id, Decimal("-100.00"), storno_of_id=original.id)
        assert is_effective_booking(partner) is False


class TestApplyStornoFilter:
    def test_normal_booking_included(self, app):
        acc, ra = _setup(app)
        b = _booking(acc.id, ra.id, Decimal("100.00"))
        db.session.commit()
        result = apply_storno_filter(Booking.query).all()
        assert any(x.id == b.id for x in result)

    def test_storno_pair_excluded_both_sides(self, app):
        acc, ra = _setup(app)
        original = _booking(acc.id, ra.id, Decimal("200.00"))
        db.session.commit()
        original.status = Booking.STATUS_STORNIERT
        partner = _booking(acc.id, ra.id, Decimal("-200.00"), storno_of_id=original.id)
        db.session.commit()

        ids = [b.id for b in apply_storno_filter(Booking.query).all()]
        assert original.id not in ids
        assert partner.id not in ids

    def test_normal_bookings_not_affected_by_storno(self, app):
        acc, ra = _setup(app)
        normal = _booking(acc.id, ra.id, Decimal("50.00"))
        original = _booking(acc.id, ra.id, Decimal("200.00"))
        db.session.commit()
        original.status = Booking.STATUS_STORNIERT
        _booking(acc.id, ra.id, Decimal("-200.00"), storno_of_id=original.id)
        db.session.commit()

        ids = [b.id for b in apply_storno_filter(Booking.query).all()]
        assert normal.id in ids


class TestJan1Balance:
    def test_empty_account_equals_opening_balance(self, app):
        ra = RealAccount(name="Leer", opening_balance=Decimal("500.00"))
        db.session.add(ra)
        db.session.commit()
        assert jan1_balance(ra, 2024) == Decimal("500.00")

    def test_prior_year_bookings_added(self, app):
        acc = Account(name="K", code="KK1")
        ra = RealAccount(name="Konto", opening_balance=Decimal("1000.00"))
        db.session.add_all([acc, ra])
        db.session.commit()

        # Buchung in 2023 → fließt in jan1_balance(2024) ein
        _booking(acc.id, ra.id, Decimal("300.00"), d=date(2023, 6, 1))
        db.session.commit()

        assert jan1_balance(ra, 2024) == Decimal("1300.00")

    def test_current_year_bookings_not_included(self, app):
        acc = Account(name="L", code="LL1")
        ra = RealAccount(name="Konto2", opening_balance=Decimal("1000.00"))
        db.session.add_all([acc, ra])
        db.session.commit()

        # Buchung in 2024 → darf NICHT in jan1_balance(2024) eingehen
        _booking(acc.id, ra.id, Decimal("300.00"), d=date(2024, 3, 1))
        db.session.commit()

        assert jan1_balance(ra, 2024) == Decimal("1000.00")

    def test_storno_pair_excluded_from_balance(self, app):
        acc = Account(name="M", code="MM1")
        ra = RealAccount(name="Konto3", opening_balance=Decimal("1000.00"))
        db.session.add_all([acc, ra])
        db.session.commit()

        # Effektive Buchung 500 + Storno-Paar 200/-200 → effektiv nur 500
        _booking(acc.id, ra.id, Decimal("500.00"), d=date(2023, 5, 1))
        b_storn = _booking(acc.id, ra.id, Decimal("200.00"), d=date(2023, 6, 1),
                           status=Booking.STATUS_STORNIERT)
        _booking(acc.id, ra.id, Decimal("-200.00"), storno_of_id=b_storn.id, d=date(2023, 6, 2))
        db.session.commit()

        # 1000 + 500 = 1500; Storno-Paar (200 + -200) zählt nicht
        assert jan1_balance(ra, 2024) == Decimal("1500.00")

    def test_year_end_balance_includes_current_year(self, app):
        acc = Account(name="N", code="NN1")
        ra = RealAccount(name="Konto4", opening_balance=Decimal("0.00"))
        db.session.add_all([acc, ra])
        db.session.commit()

        _booking(acc.id, ra.id, Decimal("400.00"), d=date(2024, 3, 1))
        db.session.commit()

        assert year_end_balance(ra, 2024) == Decimal("400.00")
