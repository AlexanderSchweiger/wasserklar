"""Reine Regeln des Anteilsbuchs und der Mitgliederliste (ohne DB)."""
from datetime import date

from app.members.member_list import STATE_FORMER, STATE_MEMBER, membership_at
from app.wg_shares import first_negative

D1, D2, D3 = date(2026, 1, 1), date(2026, 6, 30), date(2026, 12, 31)


class TestFirstNegative:
    def test_empty_and_positive(self):
        assert first_negative({}) is None
        assert first_negative({D1: 2, D2: -2}) is None

    def test_same_day_in_and_out_is_fine(self):
        # Reihenfolge innerhalb eines Tages zaehlt nicht (Tagesend-Saldo).
        assert first_negative([(D2, -1), (D2, 1)]) is None

    def test_backdated_return_breaks_later_balance(self):
        # Erwerb 1 am D2, Rueckgabe 1 rueckdatiert auf D1 → am D1 negativ.
        assert first_negative({D1: -1, D2: 1}) == (D1, -1)

    def test_future_entries_are_checked(self):
        assert first_negative({D1: 1, D3: -2}) == (D3, -1)

    def test_pairs_with_same_day_are_summed(self):
        assert first_negative([(D1, 2), (D1, -1), (D2, -2)]) == (D2, -1)


class TestMembershipAt:
    def test_member_without_dates(self):
        assert membership_at("member", None, None, D2) == STATE_MEMBER

    def test_not_yet_member(self):
        assert membership_at("member", D3, None, D2) is None

    def test_resigned_with_future_end_still_member(self):
        assert membership_at("resigned", D1, D3, D2) == STATE_MEMBER

    def test_resigned_in_the_past_is_former(self):
        assert membership_at("resigned", D1, D1, D2) == STATE_FORMER

    def test_resigned_without_date_is_former(self):
        assert membership_at("resigned", None, None, D2) == STATE_FORMER

    def test_until_includes_the_day(self):
        assert membership_at("member", None, D2, D2) == STATE_MEMBER

    def test_prospect_and_external_never_listed(self):
        assert membership_at("prospect", None, None, D2) is None
        assert membership_at("external", None, None, D2) is None


class TestSuggestedShares:
    """Regel "Flaeche je Anteil" (app/wg.py:suggested_shares)."""

    def test_rounding_modes(self):
        from app.wg import suggested_shares
        assert suggested_shares(180, 100) == 2                 # angefangene
        assert suggested_shares(180, 100, "down") == 1
        assert suggested_shares(150, 100, "nearest") == 2
        assert suggested_shares(149, 100, "nearest") == 1

    def test_exact_boundary_and_zero(self):
        from app.wg import suggested_shares
        assert suggested_shares(300, 100) == 3                 # kein Gleitkomma-Rand
        assert suggested_shares(0, 100) == 0
        assert suggested_shares("250", "62,5".replace(",", ".")) == 4

    def test_no_rule_or_area(self):
        from app.wg import suggested_shares
        assert suggested_shares(None, 100) is None
        assert suggested_shares(120, None) is None
        assert suggested_shares(120, 0) is None
        assert suggested_shares(-5, 100) is None
