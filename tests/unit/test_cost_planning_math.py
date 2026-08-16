"""Unit-Tests fuer den Rechenkern der Plankostenrechnung (ohne DB).

Die Referenzwerte sind von Hand gerechnet — Annuitaet und Sparrate sind
Standardformeln, und wenn sie kippen, kippt die ganze Beratung, die eine
Genossenschaft ihrer Hauptversammlung vorlegt.
"""
from decimal import Decimal

import pytest

from app.cost_planning import services
from app.models import FundingGoal


def _goal(**kwargs):
    """Transientes FundingGoal — nie in die Session gegeben, nur gerechnet."""
    data = dict(
        name="Test",
        goal_type=FundingGoal.TYPE_INVESTMENT,
        target_amount=Decimal("500000"),
        existing_reserve=Decimal("0"),
        interest_rate=None,
        start_year=2026,
        target_year=2030,
        status=FundingGoal.STATUS_DRAFT,
    )
    data.update(kwargs)
    return FundingGoal(**data)


# ---------------------------------------------------------------------------
# Jahresbedarf
# ---------------------------------------------------------------------------

class TestAnnualRequirement:

    def test_loan_with_interest_matches_annuity_formula(self):
        """100.000 € auf 6 Jahre bei 3,5 % -> 18.766,82 € Annuitaet."""
        goal = _goal(goal_type=FundingGoal.TYPE_LOAN,
                     target_amount=Decimal("100000"),
                     interest_rate=Decimal("3.5"),
                     start_year=2026, target_year=2031)
        res = services.annual_requirement(goal)
        assert res["years"] == 6
        assert res["amount"] == Decimal("18766.82")

    def test_loan_zero_interest_is_linear(self):
        """Ohne Zinsen muss die Annuitaet exakt P/n sein — die geschlossene
        Formel wuerde bei i = 0 durch null teilen."""
        goal = _goal(goal_type=FundingGoal.TYPE_LOAN,
                     target_amount=Decimal("120000"),
                     interest_rate=Decimal("0"),
                     start_year=2026, target_year=2031)
        res = services.annual_requirement(goal)
        assert res["amount"] == Decimal("20000.00")
        assert res["interest_total"] == Decimal("0.00")

    def test_loan_interest_none_behaves_like_zero(self):
        goal = _goal(goal_type=FundingGoal.TYPE_LOAN,
                     target_amount=Decimal("120000"), interest_rate=None,
                     start_year=2026, target_year=2031)
        assert services.annual_requirement(goal)["amount"] == Decimal("20000.00")

    def test_loan_interest_total_is_positive(self):
        goal = _goal(goal_type=FundingGoal.TYPE_LOAN,
                     target_amount=Decimal("100000"),
                     interest_rate=Decimal("3.5"),
                     start_year=2026, target_year=2031)
        res = services.annual_requirement(goal)
        # 6 x 18.766,82 = 112.600,92 -> 12.600,92 Zinsen
        assert res["interest_total"] == Decimal("12600.92")

    def test_investment_without_interest_is_linear(self):
        """500.000 € in 5 Jahren ohne Verzinsung -> glatte 100.000 €/Jahr."""
        goal = _goal(start_year=2026, target_year=2030)
        res = services.annual_requirement(goal)
        assert res["years"] == 5
        assert res["amount"] == Decimal("100000.00")

    def test_investment_with_interest_needs_less(self):
        goal = _goal(interest_rate=Decimal("2"),
                     start_year=2026, target_year=2030)
        res = services.annual_requirement(goal)
        assert res["amount"] == Decimal("96079.20")
        assert res["amount"] < Decimal("100000.00")

    def test_existing_reserve_reduces_requirement(self):
        goal = _goal(existing_reserve=Decimal("50000"),
                     start_year=2026, target_year=2030)
        res = services.annual_requirement(goal)
        assert res["amount"] == Decimal("90000.00")

    def test_existing_reserve_is_compounded(self):
        """Die vorhandene Ruecklage wird bis zum Zieljahr mitverzinst — sonst
        wuerde der Bedarf zu hoch angesetzt."""
        goal = _goal(existing_reserve=Decimal("50000"),
                     interest_rate=Decimal("2"),
                     start_year=2026, target_year=2030)
        res = services.annual_requirement(goal)
        assert res["amount"] == Decimal("85471.28")

    def test_goal_already_covered_needs_nothing(self):
        goal = _goal(target_amount=Decimal("50000"),
                     existing_reserve=Decimal("60000"))
        res = services.annual_requirement(goal)
        assert res["amount"] == Decimal("0.00")
        assert res["already_covered"] is True

    def test_single_year_runtime(self):
        goal = _goal(start_year=2026, target_year=2026)
        res = services.annual_requirement(goal)
        assert res["years"] == 1
        assert res["amount"] == Decimal("500000.00")

    def test_target_before_start_raises(self):
        goal = _goal(start_year=2030, target_year=2026)
        with pytest.raises(ValueError):
            services.annual_requirement(goal)


# ---------------------------------------------------------------------------
# Tarifpakete
# ---------------------------------------------------------------------------

def _base_info(avg_m3="40000", base_units=200, price="1.5000",
               base_fee="50.00", additional_fee=None):
    """Minimaler baseline()-Rueckgabewert; ``tariff`` wird als Fake gebaut,
    damit der Rechenkern ohne DB testbar bleibt."""
    from app.models import WaterTariff
    tariff = WaterTariff(
        name="Test", valid_from=2025,
        base_fee=Decimal(base_fee) if base_fee is not None else None,
        additional_fee=Decimal(additional_fee) if additional_fee is not None else None,
        price_per_m3=Decimal(price),
    )
    return {
        "tariff": tariff,
        "avg_m3": Decimal(avg_m3),
        "avg_years": 3,
        "basis_years": [],
        "billable_units": base_units,
        "base_fee_units": base_units,
        "additional_fee_units": 0,
        "override_units": 0,
        "current_base_revenue": Decimal("0"),
        "current_additional_revenue": Decimal("0"),
        "current_volume_revenue": Decimal("0"),
        "current_revenue": Decimal("0"),
    }


class TestBuildScenarios:

    def test_three_scenarios_are_returned(self):
        scs = services.build_scenarios(Decimal("20000"), _base_info())
        assert [s.key for s in scs] == ["base", "balanced", "volume"]

    def test_base_scenario_loads_only_the_standing_charge(self):
        """20.000 € / 200 Objekte = 100 € mehr Grundgebuehr, m³-Preis bleibt."""
        scs = services.build_scenarios(Decimal("20000"), _base_info())
        sc = next(s for s in scs if s.key == "base")
        assert sc.delta_base_fee == Decimal("100.00")
        assert sc.delta_price_per_m3 == Decimal("0")
        assert sc.base_fee == Decimal("150.00")
        assert sc.price_per_m3 == Decimal("1.5000")

    def test_volume_scenario_loads_only_the_unit_price(self):
        """20.000 € / 40.000 m³ = 0,50 € mehr je m³."""
        scs = services.build_scenarios(Decimal("20000"), _base_info())
        sc = next(s for s in scs if s.key == "volume")
        assert sc.delta_base_fee == Decimal("0")
        assert sc.delta_price_per_m3 == Decimal("0.5000")
        assert sc.price_per_m3 == Decimal("2.0000")
        assert sc.base_fee == Decimal("50.00")

    def test_balanced_scenario_splits_in_half(self):
        scs = services.build_scenarios(Decimal("20000"), _base_info())
        sc = next(s for s in scs if s.key == "balanced")
        assert sc.delta_base_fee == Decimal("50.00")
        assert sc.delta_price_per_m3 == Decimal("0.2500")

    def test_every_scenario_covers_the_requirement(self):
        for sc in services.build_scenarios(Decimal("20000"), _base_info()):
            assert sc.realized == Decimal("20000.00")
            assert sc.coverage == Decimal("100.00")
            assert sc.feasible

    def test_sample_household_effect(self):
        """Verbrauchslastig: 120 m³ x 0,50 € = 60 € pro Jahr = 5 € im Monat."""
        scs = services.build_scenarios(Decimal("20000"), _base_info(),
                                       sample_household_m3=Decimal("120"))
        sc = next(s for s in scs if s.key == "volume")
        assert sc.sample_delta_year == Decimal("60.00")
        assert sc.sample_delta_month == Decimal("5.00")

    def test_rounding_to_full_euro_creates_a_visible_gap(self):
        """Runden auf volle Euro deckt den Bedarf nicht mehr exakt — genau das
        soll der Deckungsgrad offenlegen statt Scheingenauigkeit zu behaupten."""
        info = _base_info(base_units=300)
        scs = services.build_scenarios(Decimal("20000"), info, rounding="euro")
        sc = next(s for s in scs if s.key == "base")
        # 20.000 / 300 = 66,67 -> gerundet 67 -> 300 x 67 = 20.100
        assert sc.delta_base_fee == Decimal("67")
        assert sc.realized == Decimal("20100.00")
        assert sc.coverage > Decimal("100")
        assert sc.gap == Decimal("100.00")

    def test_rounding_can_undershoot(self):
        info = _base_info(base_units=300)
        scs = services.build_scenarios(Decimal("19900"), info, rounding="euro")
        sc = next(s for s in scs if s.key == "base")
        # 19.900 / 300 = 66,33 -> gerundet 66 -> 300 x 66 = 19.800
        assert sc.realized == Decimal("19800.00")
        assert sc.coverage < Decimal("100")
        assert sc.gap == Decimal("-100.00")

    def test_no_tariff_base_fee_units_makes_base_scenario_infeasible(self):
        """Wenn kein Objekt seine Grundgebuehr aus dem Tarif bezieht, ist ein
        Grundgebuehr-Aufschlag unmoeglich — und darf nicht durch null teilen."""
        info = _base_info(base_units=0)
        scs = services.build_scenarios(Decimal("20000"), info)
        sc = next(s for s in scs if s.key == "base")
        assert not sc.feasible
        assert sc.warnings
        assert sc.delta_base_fee == Decimal("0")

    def test_no_consumption_makes_volume_scenario_infeasible(self):
        info = _base_info(avg_m3="0")
        scs = services.build_scenarios(Decimal("20000"), info)
        sc = next(s for s in scs if s.key == "volume")
        assert not sc.feasible
        assert sc.delta_price_per_m3 == Decimal("0")
        # Das reine Grundgebuehr-Paket funktioniert weiterhin.
        assert next(s for s in scs if s.key == "base").feasible

    def test_zero_requirement_is_fully_covered(self):
        scs = services.build_scenarios(Decimal("0"), _base_info())
        for sc in scs:
            assert sc.realized == Decimal("0.00")
            assert sc.coverage == Decimal("100.00")

    def test_tariff_without_base_fee_still_gets_one_when_surcharged(self):
        """Hat der Tarif gar keine Grundgebuehr, entsteht durch das
        grundgebuehr-lastige Paket eine neue."""
        info = _base_info(base_fee=None)
        scs = services.build_scenarios(Decimal("20000"), info)
        assert next(s for s in scs if s.key == "base").base_fee == Decimal("100.00")
        # Beim verbrauchslastigen Paket bleibt es bei "keine Grundgebuehr".
        assert next(s for s in scs if s.key == "volume").base_fee is None


# ---------------------------------------------------------------------------
# Projektion
# ---------------------------------------------------------------------------

class TestProjection:

    def test_investment_reaches_the_target(self):
        goal = _goal(start_year=2026, target_year=2030)
        scs = services.build_scenarios(Decimal("100000"), _base_info())
        rows = services.projection(goal, scs[0])
        assert len(rows) == 5
        assert rows[0]["year"] == 2026
        assert rows[-1]["balance"] == Decimal("500000.00")

    def test_investment_starts_from_existing_reserve(self):
        goal = _goal(existing_reserve=Decimal("50000"),
                     start_year=2026, target_year=2030)
        scs = services.build_scenarios(Decimal("90000"), _base_info())
        rows = services.projection(goal, scs[0])
        assert rows[0]["balance"] == Decimal("140000.00")
        assert rows[-1]["balance"] == Decimal("500000.00")

    def test_loan_is_paid_off(self):
        """Deckt das Paket den Bedarf voll (hier: ein einziges Objekt, also
        kein Rundungsverlust bei der Verteilung), ist die Schuld am Zieljahr
        getilgt."""
        goal = _goal(goal_type=FundingGoal.TYPE_LOAN,
                     target_amount=Decimal("100000"),
                     interest_rate=Decimal("3.5"),
                     start_year=2026, target_year=2031)
        req = services.annual_requirement(goal)
        scs = services.build_scenarios(req["amount"], _base_info(base_units=1))
        assert scs[0].realized == req["amount"]
        rows = services.projection(goal, scs[0])
        assert len(rows) == 6
        # Restschuld am Ende praktisch null (Cent-Rundung je Jahr).
        assert Decimal("0") <= rows[-1]["balance"] <= Decimal("0.05")

    def test_rounding_gap_leaves_a_residual_debt(self):
        """Deckt das Paket nach der Rundung nur 99,99 % ab, bleibt am Ende ein
        Rest stehen — das ist kein Rechenfehler, sondern genau die Information,
        die der Deckungsgrad ausweist."""
        goal = _goal(goal_type=FundingGoal.TYPE_LOAN,
                     target_amount=Decimal("100000"),
                     interest_rate=Decimal("3.5"),
                     start_year=2026, target_year=2031)
        req = services.annual_requirement(goal)
        scs = services.build_scenarios(req["amount"], _base_info(base_units=200))
        sc = scs[0]
        assert sc.realized < req["amount"]        # 200 x 93,83 = 18.766,00
        rows = services.projection(goal, sc)
        assert rows[-1]["balance"] > Decimal("0")

    def test_loan_first_year_interest(self):
        goal = _goal(goal_type=FundingGoal.TYPE_LOAN,
                     target_amount=Decimal("100000"),
                     interest_rate=Decimal("3.5"),
                     start_year=2026, target_year=2031)
        req = services.annual_requirement(goal)
        scs = services.build_scenarios(req["amount"], _base_info(base_units=1))
        rows = services.projection(goal, scs[0])
        assert rows[0]["interest"] == Decimal("3500.00")
        assert rows[0]["principal"] == Decimal("15266.82")

    def test_loan_balance_never_goes_negative(self):
        """Die letzte Rate wird gekappt — eine negative Restschuld waere
        Unsinn auf einer Beschlussvorlage."""
        goal = _goal(goal_type=FundingGoal.TYPE_LOAN,
                     target_amount=Decimal("10000"),
                     interest_rate=Decimal("0"),
                     start_year=2026, target_year=2028)
        # Bewusst zu hoher Beitrag (deckt mehr als noetig).
        scs = services.build_scenarios(Decimal("9000"), _base_info())
        rows = services.projection(goal, scs[0])
        assert all(r["balance"] >= Decimal("0") for r in rows)
        assert rows[-1]["balance"] == Decimal("0.00")
