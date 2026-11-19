"""Faelligkeiten und Teilzeitraeume der Teilrechnungen (rein, ohne DB)."""
from datetime import date

import pytest

from app.installments.schedule import (
    ScheduleError, dates_for_period, parse_due_dates, part_of_period,
)


class TestDueDates:
    def test_parse_and_map_onto_calendar_year(self):
        spec = parse_due_dates("15.02.;15.05.;15.08.;15.11.")
        assert dates_for_period(spec, date(2026, 1, 1), date(2026, 12, 31)) == [
            date(2026, 2, 15), date(2026, 5, 15), date(2026, 8, 15), date(2026, 11, 15)]

    def test_period_starting_in_april(self):
        """Wie der Kanal: Abrechnungsjahr 01.04.–31.03."""
        spec = parse_due_dates("15.08.;15.11.;15.02.")
        assert dates_for_period(spec, date(2025, 4, 1), date(2026, 3, 31)) == [
            date(2025, 8, 15), date(2025, 11, 15), date(2026, 2, 15)]

    @pytest.mark.parametrize("text", ["", "31.02.", "15.02.;15.02.", "1502"])
    def test_invalid(self, text):
        with pytest.raises(ScheduleError):
            parse_due_dates(text)


class TestPartOfPeriod:
    def test_quarters(self):
        start, end = date(2026, 1, 1), date(2026, 12, 31)
        assert [part_of_period(start, end, 4, k) for k in range(1, 5)] == [
            (date(2026, 1, 1), date(2026, 3, 31)), (date(2026, 4, 1), date(2026, 6, 30)),
            (date(2026, 7, 1), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 12, 31))]

    def test_quarters_across_the_year_end(self):
        """Periode 01.04.–31.03.: der dritte Teil ist 01.10.–31.12. (wie die Kanal-Vorschreibung)."""
        assert part_of_period(date(2025, 4, 1), date(2026, 3, 31), 4, 3) == (
            date(2025, 10, 1), date(2025, 12, 31))
        assert part_of_period(date(2025, 4, 1), date(2026, 3, 31), 4, 4) == (
            date(2026, 1, 1), date(2026, 3, 31))

    def test_months_and_halves(self):
        start, end = date(2026, 1, 1), date(2026, 12, 31)
        assert part_of_period(start, end, 12, 2) == (date(2026, 2, 1), date(2026, 2, 28))
        assert part_of_period(start, end, 2, 2) == (date(2026, 7, 1), date(2026, 12, 31))

    def test_uneven_split_by_days_without_gaps(self):
        start, end = date(2026, 1, 1), date(2026, 12, 31)
        parts = [part_of_period(start, end, 5, k) for k in range(1, 6)]
        assert parts[0][0] == start and parts[-1][1] == end
        for (_a, b), (c, _d) in zip(parts, parts[1:]):
            assert (c - b).days == 1

    @pytest.mark.parametrize("count,seq", [(0, 1), (4, 0), (4, 5)])
    def test_invalid_seq(self, count, seq):
        assert part_of_period(date(2026, 1, 1), date(2026, 12, 31), count, seq) is None
