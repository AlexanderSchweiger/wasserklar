"""Reine Regeln des Festschreibens (``app/accounting/posting.py``): Zeitraum, Sperrtag, Stichtag."""
from datetime import date

import pytest

from app.accounting import posting as p

M, Q = p.PERIOD_MONTH, p.PERIOD_QUARTER


class TestPeriods:
    @pytest.mark.parametrize("d, period, end", [
        (date(2027, 2, 14), M, date(2027, 2, 28)),
        (date(2028, 2, 14), M, date(2028, 2, 29)),
        (date(2027, 1, 1), Q, date(2027, 3, 31)),
        (date(2027, 6, 30), Q, date(2027, 6, 30)),
        (date(2027, 11, 3), Q, date(2027, 12, 31)),
    ])
    def test_period_end(self, d, period, end):
        assert p.period_end_for(d, period) == end

    def test_previous_and_next(self):
        assert p.previous_period_end(date(2027, 3, 31), Q) == date(2026, 12, 31)
        assert p.previous_period_end(date(2027, 1, 31), M) == date(2026, 12, 31)
        assert p.next_period_end(date(2026, 12, 31), Q) == date(2027, 3, 31)
        assert p.next_period_end(date(2027, 1, 31), M) == date(2027, 2, 28)


class TestLockDate:
    def test_at_quarter(self):
        assert p.lock_date(date(2027, 3, 31), 15, 1) == date(2027, 4, 15)

    def test_de_month_and_extension(self):
        assert p.lock_date(date(2027, 1, 31), 10, 1) == date(2027, 2, 10)
        assert p.lock_date(date(2027, 1, 31), 10, 2) == date(2027, 3, 10)

    def test_year_boundary(self):
        assert p.lock_date(date(2026, 12, 31), 15, 1) == date(2027, 1, 15)
        assert p.lock_date(date(2026, 12, 31), 15, 2) == date(2027, 2, 15)
        assert p.lock_date(date(2026, 11, 30), 28, 2) == date(2027, 1, 28)


class TestCutoff:
    def test_on_lock_day_the_period_stays_open(self):
        # Q1 wird am Ende des 15.04. verbucht: am 15. selbst noch offen, Stichtag = Ende Q4
        assert p.cutoff(date(2027, 4, 15), p.MODE_PERIOD, Q, 15, 1) == date(2026, 12, 31)

    def test_day_after_lock_day(self):
        assert p.cutoff(date(2027, 4, 16), p.MODE_PERIOD, Q, 15, 1) == date(2027, 3, 31)

    def test_inside_a_quarter(self):
        assert p.cutoff(date(2027, 6, 1), p.MODE_PERIOD, Q, 15, 1) == date(2027, 3, 31)
        assert p.cutoff(date(2027, 1, 10), p.MODE_PERIOD, Q, 15, 1) == date(2026, 9, 30)
        assert p.cutoff(date(2027, 1, 16), p.MODE_PERIOD, Q, 15, 1) == date(2026, 12, 31)

    def test_monthly_with_second_following_month(self):
        # Dezember-UVA mit Dauerfristverlängerung (10. des zweitfolgenden Monats)
        assert p.cutoff(date(2027, 2, 10), p.MODE_PERIOD, M, 10, 2) == date(2026, 11, 30)
        assert p.cutoff(date(2027, 2, 11), p.MODE_PERIOD, M, 10, 2) == date(2026, 12, 31)

    def test_daily_and_off(self):
        assert p.cutoff(date(2027, 4, 16), p.MODE_DAILY, Q, 15, 1) == date(2027, 4, 15)
        assert p.cutoff(date(2027, 4, 16), p.MODE_OFF, Q, 15, 1) is None

    def test_lock_date_for_booking(self):
        assert p.lock_date_for(date(2027, 2, 3), p.MODE_PERIOD, Q, 15, 1) == date(2027, 4, 15)
        assert p.lock_date_for(date(2027, 2, 3), p.MODE_DAILY, Q, 15, 1) == date(2027, 2, 3)
        assert p.lock_date_for(date(2027, 2, 3), p.MODE_OFF, Q, 15, 1) is None


class TestParse:
    @pytest.mark.parametrize("raw, value", [("15", 15), (" 1 ", 1), ("28", 28), ("29", None),
                                            ("0", None), ("", None), (None, None), ("x", None)])
    def test_lock_day(self, raw, value):
        assert p.parse_lock_day(raw) == value

    @pytest.mark.parametrize("raw, value", [("1", 1), ("2", 2), ("3", None), ("", None)])
    def test_offset(self, raw, value):
        assert p.parse_lock_offset(raw) == value
