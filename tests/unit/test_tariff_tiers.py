"""Staffelpreise: reine Rechenregeln der Tarif-Engine + Datenform (tariff_spec).

Beispiel aus der Anforderung: bis 200 m³ 1,20 €, über 200 bis 500 m³ 1,50 €,
über 500 m³ 2,00 € — anteilig (graduated) bzw. Gesamtmenge zum Stufenpreis
(whole).
"""
from datetime import date
from decimal import Decimal

import pytest

from app.invoices import tariff_engine as engine
from app.invoices import tariff_spec as spec
from app.invoices.tariff_spec import TierStep
from app.models import ChargeType

D = Decimal
STEPS = (TierStep(D("200"), D("1.50")), TierStep(D("500"), D("2.00")))


def _water(mode=spec.TIER_GRADUATED, steps=STEPS, valid_from=None, key="water"):
    return engine.make_charge(key=key, label="Wasserverbrauch",
                              calc_type=ChargeType.CALC_PER_M3, amount=D("1.20"),
                              tiers=steps, tier_mode=mode, valid_from=valid_from)


def _amounts(lines):
    return [l["amount"] for l in lines]


class TestTierBands:
    @pytest.mark.parametrize("qty, expected", [
        (D("0"), [(1, D("0"))]),
        (D("150"), [(1, D("150"))]),
        (D("200"), [(1, D("200"))]),                         # Grenze gehört zur unteren Stufe
        (D("200.001"), [(1, D("200")), (2, D("0.001"))]),
        (D("500"), [(1, D("200")), (2, D("300"))]),
        (D("550"), [(1, D("200")), (2, D("300")), (3, D("50"))]),
    ])
    def test_graduated(self, qty, expected):
        bands = engine.tier_bands(_water(), qty)
        assert [(b.index, b.qty) for b in bands] == expected

    @pytest.mark.parametrize("qty, index, price", [
        (D("150"), 1, D("1.20")),
        (D("200"), 1, D("1.20")),
        (D("200.5"), 2, D("1.50")),
        (D("500"), 2, D("1.50")),
        (D("550"), 3, D("2.00")),
    ])
    def test_whole(self, qty, index, price):
        [band] = engine.tier_bands(_water(spec.TIER_WHOLE), qty)
        assert (band.index, band.qty, band.price) == (index, qty, price)

    def test_negative_quantity_uses_first_price(self):
        [band] = engine.tier_bands(_water(), D("-12"))
        assert (band.qty, band.price) == (D("-12"), D("1.20"))

    def test_without_tiers_one_open_band(self):
        [band] = engine.tier_bands(_water(steps=()), D("900"))
        assert (band.qty, band.price, band.upper) == (D("900"), D("1.20"), None)

    def test_flat_charge_never_tiered(self):
        fee = engine.make_charge(key="base_fee", label="Grundgebühr",
                                 calc_type=ChargeType.CALC_FLAT, amount=D("30"), tiers=STEPS)
        assert fee.tiers == () and not fee.is_tiered


class TestVolumeLines:
    def test_linear_text_unchanged(self):
        [line] = engine.volume_lines(_water(steps=()), D("120"), head="Wasserverbrauch 2026")
        assert line["description"] == "Wasserverbrauch 2026 (120 m³ × 1,2000 €/m³)"
        assert line["amount"] == D("144.00")

    def test_graduated_one_line_per_band(self):
        lines = engine.volume_lines(_water(), D("550"), head="Wasserverbrauch 2026",
                                    detail=" – abzüglich 5 m³")
        assert [l["quantity"] for l in lines] == [D("200"), D("300"), D("50")]
        assert [l["unit_price"] for l in lines] == [D("1.20"), D("1.50"), D("2.00")]
        assert _amounts(lines) == [D("240.00"), D("450.00"), D("100.00")]
        assert lines[0]["description"] == (
            "Wasserverbrauch 2026 – Stufe 1 (bis 200 m³): 200 m³ × 1,2000 €/m³ – abzüglich 5 m³")
        assert lines[1]["description"] == (
            "Wasserverbrauch 2026 – Stufe 2 (über 200 bis 500 m³): 300 m³ × 1,5000 €/m³")
        assert lines[2]["description"] == (
            "Wasserverbrauch 2026 – Stufe 3 (über 500 m³): 50 m³ × 2,0000 €/m³")
        assert all(l["charge_key"] == "water" for l in lines)

    def test_whole_one_line_at_reached_price(self):
        [line] = engine.volume_lines(_water(spec.TIER_WHOLE), D("550"), head="Wasser")
        assert line["amount"] == D("1100.00")
        assert line["description"] == (
            "Wasser (550 m³ × 2,0000 €/m³, Stufenpreis über 500 m³)")

    def test_whole_jump_at_threshold(self):
        """Die gewünschte Sprungstelle: 200 m³ kosten weniger als 201 m³ × Stufe 2."""
        low = engine.volume_amount(_water(spec.TIER_WHOLE), D("200"))
        high = engine.volume_amount(_water(spec.TIER_WHOLE), D("201"))
        assert (low, high) == (D("240.00"), D("301.50"))

    def test_apportioned_after_tier_split(self):
        """valid_from: Stufe aus der ganzen Menge, danach je Stufe nach Tagen."""
        charge = _water(valid_from=date(2026, 7, 1), key="water_levy")
        lines = engine.volume_lines(charge, D("550"), head="Abgabe",
                                    start=date(2026, 1, 1), end=date(2026, 12, 31))
        assert [l["quantity"] for l in lines] == [D("100.822"), D("151.233"), D("25.205")]
        assert "zeitanteilig ab 01.07.2026: 184/365 Tage" in lines[0]["description"]

    def test_zero_consumption_single_line(self):
        [line] = engine.volume_lines(_water(), D("0"), head="Wasser")
        assert line["amount"] == D("0.00") and "Stufe" not in line["description"]


class TestBuildLinesAndTotals:
    def test_meter_parts_with_tiers_staggered_on_sum(self):
        case = engine.BillingCase(vat_liable=True, period_name="2026", meter_parts=[
            engine.MeterPart(label="Zähler A", qty=D("300")),
            engine.MeterPart(label="Zähler B", qty=D("250"), is_estimated=True)],
            is_estimated=True)
        lines = engine.build_lines([_water()], case)
        assert [l["quantity"] for l in lines] == [D("200"), D("300"), D("50")]
        assert "Zähler A: 300 m³, Zähler B: 250 m³" in lines[0]["description"]

    def test_meter_parts_linear_one_line_per_meter(self):
        case = engine.BillingCase(vat_liable=False, period_name="2026", meter_parts=[
            engine.MeterPart(label="Zähler A", qty=D("30"), note="ausgebaut 01.03.2026"),
            engine.MeterPart(label="Zähler B", qty=D("70"))])
        lines = engine.build_lines([_water(steps=())], case)
        assert [l["description"] for l in lines] == [
            "Wasserverbrauch 2026 – Zähler A (ausgebaut 01.03.2026, 30 m³)",
            "Wasserverbrauch 2026 – Zähler B (70 m³)"]

    def test_totals_round_per_line(self):
        lines = [{"amount": D("10.05"), "tax_rate": D("7")},
                 {"amount": D("10.05"), "tax_rate": D("7")},
                 {"amount": D("5.00"), "tax_rate": None}]
        sums = engine.totals(lines)
        # 0,7035 → 0,70 je Position (wie Invoice.recalculate_total), nicht 1,41 gesamt
        assert sums["taxes"][D("7")] == {"net": D("20.10"), "tax": D("1.40")}
        assert (sums["net"], sums["tax"], sums["gross"]) == (D("25.10"), D("1.40"), D("26.50"))


class TestSpec:
    def test_tiers_roundtrip(self):
        text = spec.dump_tiers(STEPS)
        assert text == '[{"above": "200", "price": "1.5"}, {"above": "500", "price": "2"}]'
        assert spec.parse_tiers(text) == STEPS
        assert spec.dump_tiers(()) is None and spec.parse_tiers(None) == ()

    @pytest.mark.parametrize("steps, message", [
        ((TierStep(D("0"), D("1")),), "größer als 0"),
        ((TierStep(D("500"), D("1")), TierStep(D("200"), D("2"))), "steigen"),
        ((TierStep(D("200"), D("-1")),), "negativ"),
        ((TierStep(D("200"), D("1.23456")),), "vier Nachkommastellen"),
        ((TierStep(D("200.0001"), D("1")),), "drei Nachkommastellen"),
        (tuple(TierStep(D(n + 1), D("1")) for n in range(11)), "Höchstens 10"),
    ])
    def test_tiers_rejected(self, steps, message):
        with pytest.raises(ValueError, match=message):
            spec.check_tiers(steps)

    def test_broken_json_is_loud(self):
        with pytest.raises(ValueError):
            spec.parse_tiers("{kaputt")
        with pytest.raises(ValueError):
            spec.parse_tiers('[{"above": "abc", "price": "1"}]')

    def test_conditions(self):
        assert spec.dump_conditions({"contact_status": ["external", "member"]}) == (
            '{"contact_status": ["member", "external"]}')          # kanonische Reihenfolge
        assert spec.contact_statuses('{"contact_status": ["member"]}') == frozenset({"member"})
        assert spec.contact_statuses(None) is None
        assert spec.condition_text({"contact_status": ["member", "prospect"]}) == (
            "nur Interessent, Mitglied")
        with pytest.raises(ValueError, match="Unbekannter Status"):
            spec.parse_conditions({"contact_status": ["vip"]})
        with pytest.raises(ValueError, match="Unbekannte Bedingung"):
            spec.parse_conditions({"object_type": ["Haus"]})

    def test_levels_text(self):
        assert spec.levels_text(D("1.2"), STEPS, spec.TIER_WHOLE) == (
            "bis 200 m³: 1,2000 · über 200 bis 500 m³: 1,5000 · über 500 m³: 2,0000 €/m³ "
            "(Gesamtmenge zum Stufenpreis)")
        assert spec.levels_text(D("1.2"), [{"above": "1000", "price": "1"}]) == (
            "bis 1.000 m³: 1,2000 · über 1.000 m³: 1,0000 €/m³ (anteilig)")
        assert spec.levels_text(D("1.2"), None) == ""


class TestConditionMet:
    def test_rules(self):
        members = frozenset({"member"})
        assert engine.condition_met(members, "member", True)
        assert not engine.condition_met(members, "external", True)
        assert engine.condition_met(members, "external", False)    # Versorger-Modus
        assert engine.condition_met(members, None, True)           # reine Tarifansicht
        assert engine.condition_met(None, "external", True)

    def test_water_never_conditioned(self):
        water = engine.make_charge(key="water", label="W", calc_type=ChargeType.CALC_PER_M3,
                                   amount=D("1"), statuses={"member"})
        assert water.statuses is None
