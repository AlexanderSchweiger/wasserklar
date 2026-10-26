"""Mindestabnahme: reine Rechenregeln der Tarif-Engine + Datenform (tariff_spec).

Beispiel aus der Anforderung: 50 m³ Mindestabnahme — wer weniger verbraucht,
zahlt trotzdem 50 m³; die Differenz steht als eigene Auffüllzeile auf der
Rechnung, die Verbrauchszeile bleibt der echte Verbrauch.
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


def _charge(*, minimum="50", steps=(), mode=spec.TIER_GRADUATED, key="water",
            price="1.40", valid_from=None):
    return engine.make_charge(key=key, label="Wasserbezug",
                              calc_type=ChargeType.CALC_PER_M3, amount=D(price),
                              tiers=steps, tier_mode=mode, min_qty=minimum,
                              valid_from=valid_from)


def _lines(charge, qty, **kw):
    minimum = kw.pop("minimum", charge.min_qty)
    return engine.volume_lines(charge, D(str(qty)), head="Wasserbezug 2025/26",
                               minimum=minimum, **kw)


def _fill(lines):
    return [l for l in lines if l["is_minimum_fill"]]


def _used(lines):
    return [l for l in lines if not l["is_minimum_fill"]]


class TestFillQuantity:
    @pytest.mark.parametrize("qty, minimum, fill", [
        ("30", "50", "20"),
        ("50", "50", "0"),
        ("80", "50", "0"),
        ("0", "50", "50"),
        ("-10", "50", "50"),       # negativer Verbrauch zaehlt als 0
        ("30", None, "0"),
    ])
    def test_fill(self, qty, minimum, fill):
        minimum = D(minimum) if minimum else None
        assert engine.fill_quantity(D(qty), minimum) == D(fill)

    def test_billed_quantity(self):
        assert engine.billed_quantity(D("30"), D("50")) == D("50")
        assert engine.billed_quantity(D("-10"), D("50")) == D("40")
        assert engine.billed_quantity(D("80"), D("50")) == D("80")


class TestLinear:
    def test_fill_line_below_minimum(self):
        lines = _lines(_charge(), 30)
        [used] = _used(lines)
        [fill] = _fill(lines)
        assert (used["quantity"], used["amount"]) == (D("30"), D("42.00"))
        assert (fill["quantity"], fill["unit_price"], fill["amount"]) == (
            D("20"), D("1.40"), D("28.00"))
        assert fill["description"] == (
            "Wasserbezug 2025/26 – Mindestabnahme 50 m³ (verbraucht 30 m³): "
            "20 m³ × 1,40 €/m³")
        assert all(l["min_quantity"] == D("50") for l in lines)
        assert fill["charge_key"] == "water" and fill["unit"] == "m³"

    def test_no_fill_at_or_above_minimum(self):
        lines = _lines(_charge(), 50)
        assert not _fill(lines)
        # Die Mindestmenge ist trotzdem eingefroren (Schaetzkorrektur).
        assert lines[0]["min_quantity"] == D("50")

    def test_without_minimum_lines_unchanged(self):
        lines = _lines(_charge(minimum=None), 30)
        assert len(lines) == 1
        assert lines[0]["min_quantity"] is None and not lines[0]["is_minimum_fill"]
        assert lines[0]["description"] == "Wasserbezug 2025/26 (30 m³ × 1,40 €/m³)"

    def test_zero_consumption_pays_minimum(self):
        lines = _lines(_charge(), 0)
        assert [l["amount"] for l in lines] == [D("0.00"), D("70.00")]

    def test_negative_consumption_keeps_credit(self):
        lines = _lines(_charge(), -10)
        assert [(l["quantity"], l["amount"]) for l in lines] == [
            (D("-10"), D("-14.00")), (D("50"), D("70.00"))]

    def test_volume_amount_includes_fill(self):
        charge = _charge()
        assert engine.volume_amount(charge, D("30"), minimum=D("50")) == D("70.00")
        assert engine.volume_amount(charge, D("30")) == D("42.00")
        assert engine.volume_amount(charge, D("60"), minimum=D("50")) == D("84.00")


class TestTiers:
    def test_graduated_fill_spans_stages(self):
        # Mindestmenge 250 m³ bei Verbrauch 150: Auffuellung belegt 150–200
        # (Stufe 1) und 200–250 (Stufe 2).
        charge = _charge(minimum="250", steps=STEPS, price="1.20")
        lines = _lines(charge, 150)
        [used] = _used(lines)
        fills = _fill(lines)
        assert (used["quantity"], used["unit_price"]) == (D("150"), D("1.20"))
        assert [(l["quantity"], l["unit_price"]) for l in fills] == [
            (D("50"), D("1.20")), (D("50"), D("1.50"))]
        assert fills[0]["description"] == (
            "Wasserbezug 2025/26 – Mindestabnahme 250 m³ (verbraucht 150 m³), "
            "Stufe 1 (bis 200 m³): 50 m³ × 1,20 €/m³")
        assert fills[1]["description"] == (
            "Wasserbezug 2025/26 – Mindestabnahme 250 m³, "
            "Stufe 2 (über 200 bis 500 m³): 50 m³ × 1,50 €/m³")
        # Summe = Staffel auf die abgerechnete Menge
        assert sum(l["amount"] for l in lines) == engine.volume_amount(
            _charge(minimum=None, steps=STEPS, price="1.20"), D("250"))

    def test_whole_uses_stage_of_billed_quantity(self):
        charge = _charge(minimum="250", steps=STEPS, price="1.20", mode=spec.TIER_WHOLE)
        lines = _lines(charge, 150)
        assert [(l["quantity"], l["unit_price"], l["is_minimum_fill"]) for l in lines] == [
            (D("150"), D("1.50"), False), (D("100"), D("1.50"), True)]
        assert lines[1]["description"] == (
            "Wasserbezug 2025/26 – Mindestabnahme 250 m³ (verbraucht 150 m³): "
            "100 m³ × 1,50 €/m³, Stufenpreis über 200 bis 500 m³")

    def test_above_minimum_tiers_as_before(self):
        charge = _charge(minimum="50", steps=STEPS, price="1.20")
        lines = _lines(charge, 300)
        assert not _fill(lines)
        assert [l["quantity"] for l in lines] == [D("200"), D("100")]


class TestShares:
    def test_valid_from_scales_fill_too(self):
        # Position gilt ab 1.7. (184/365 Tage): Verbrauch und Auffuellung
        # werden gleich abgegrenzt, keine zweite Kuerzung der Mindestmenge.
        charge = _charge(key="custom_1", valid_from=date(2026, 7, 1))
        lines = engine.volume_lines(charge, D("30"), head="Kanal", minimum=D("50"),
                                    start=date(2026, 1, 1), end=date(2026, 12, 31))
        [used] = _used(lines)
        [fill] = _fill(lines)
        assert used["quantity"] == (D("30") * 184 / 365).quantize(D("0.001"))
        assert fill["quantity"] == (D("20") * 184 / 365).quantize(D("0.001"))
        assert "zeitanteilig ab 01.07.2026: 184/365 Tage" in fill["description"]

    def test_effective_minimum_share(self):
        charge = _charge()
        full, note = engine.effective_minimum(charge, engine.BillingCase())
        assert (full, note) == (D("50"), "")
        part, note = engine.effective_minimum(charge, engine.BillingCase(min_share=(182, 365)))
        assert part == D("24.932")
        assert note == "anteilig 182/365 Tage von 50 m³"
        assert engine.effective_minimum(charge, engine.BillingCase(min_share=(0, 365))) == (None, "")
        assert engine.effective_minimum(charge, engine.BillingCase(min_share=(365, 365))) == (D("50"), "")

    def test_prorated_note_in_text(self):
        lines = engine.build_lines([_charge()], engine.BillingCase(
            consumption=D("10"), period_name="2025/26", min_share=(182, 365)))
        [fill] = _fill(lines)
        assert fill["quantity"] == D("14.932")
        assert fill["description"] == (
            "Wasserbezug 2025/26 – Mindestabnahme 24,932 m³ "
            "(anteilig 182/365 Tage von 50 m³; verbraucht 10 m³): "
            "14,932 m³ × 1,40 €/m³")


class TestBuildLines:
    def test_meter_parts_linear_one_fill_line(self):
        case = engine.BillingCase(period_name="2025/26", meter_parts=[
            engine.MeterPart(label="Zähler 1", qty=D("10"), is_estimated=True),
            engine.MeterPart(label="Zähler 2", qty=D("15"))])
        lines = engine.build_lines([_charge()], case)
        assert [(l["quantity"], l["is_minimum_fill"]) for l in lines] == [
            (D("10"), False), (D("15"), False), (D("25"), True)]
        assert lines[2]["is_estimated"] is False
        assert "(verbraucht 25 m³)" in lines[2]["description"]

    def test_fill_line_never_estimated(self):
        case = engine.BillingCase(consumption=D("30"), is_estimated=True)
        lines = engine.build_lines([_charge()], case)
        assert [l["is_estimated"] for l in lines] == [True, False]

    def test_other_m3_position_own_minimum(self):
        water = _charge(minimum=None)
        sewer = _charge(key="custom_1", minimum="40", price="2.00")
        lines = engine.build_lines([water, sewer], engine.BillingCase(consumption=D("30")))
        assert [(l["charge_key"], l["quantity"], l["is_minimum_fill"]) for l in lines] == [
            ("water", D("30"), False), ("custom_1", D("30"), False),
            ("custom_1", D("10"), True)]

    def test_minimum_only_on_m3(self):
        flat = engine.make_charge(key="base_fee", label="Grundgebühr",
                                  calc_type=ChargeType.CALC_FLAT, amount=D("30"),
                                  min_qty="50")
        assert flat.min_qty is None


class TestSpec:
    @pytest.mark.parametrize("raw, typed, expected", [
        ("50", True, D("50")),
        ("12,5", True, D("12.5")),
        ("1.000", True, D("1000")),        # Tausenderpunkt im Formular
        ("1.000,5", True, D("1000.5")),
        ("50.125", False, D("50.125")),    # Snapshot: Punkt = Dezimalzeichen
        ("", True, None),
        ("0", True, None),
        (None, False, None),
        (D("50"), False, D("50")),
    ])
    def test_parse(self, raw, typed, expected):
        assert spec.parse_min_quantity(raw, typed=typed) == expected

    @pytest.mark.parametrize("raw", ["abc", "-5", "1,2345"])
    def test_parse_invalid(self, raw):
        with pytest.raises(ValueError):
            spec.parse_min_quantity(raw, typed=True)

    def test_minimum_text(self):
        assert spec.minimum_text(D("50")) == "Mindestabnahme 50 m³"
        assert spec.minimum_text("12.5") == "Mindestabnahme 12,5 m³"
        assert spec.minimum_text(None) == ""
        assert spec.minimum_text("kaputt") == ""
