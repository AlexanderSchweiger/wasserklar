"""Unit-Tests fuer die Eigentuemerwechsel-Mathematik (ohne DB)."""
from datetime import date
from types import SimpleNamespace

from app.owner_change.services import fee_day_split


def _period(start, end):
    return SimpleNamespace(start_date=start, end_date=end)


class TestFeeDaySplit:
    def test_sums_to_period_days_leap_year(self):
        p = _period(date(2024, 1, 1), date(2024, 12, 31))  # 366 Tage
        old, new, total = fee_day_split(p, date(2024, 7, 1))
        assert total == 366
        assert old + new == total
        assert old == 182  # 1.1. bis 30.6. (Stichtag zaehlt zum Neuen)

    def test_stichtag_at_period_start(self):
        p = _period(date(2025, 1, 1), date(2025, 12, 31))  # 365 Tage
        old, new, total = fee_day_split(p, date(2025, 1, 1))
        assert old == 0
        assert new == 365
        assert old + new == total == 365

    def test_stichtag_at_period_end(self):
        p = _period(date(2025, 1, 1), date(2025, 12, 31))
        old, new, total = fee_day_split(p, date(2025, 12, 31))
        assert new == 1              # nur der letzte Tag zaehlt zum Neuen
        assert old == 364
        assert old + new == total == 365

    def test_offset_period(self):
        # Verschobene Periode (Juni–Juni).
        p = _period(date(2025, 6, 1), date(2026, 5, 31))
        old, new, total = fee_day_split(p, date(2025, 9, 1))
        assert old + new == total
        assert old == (date(2025, 9, 1) - date(2025, 6, 1)).days
