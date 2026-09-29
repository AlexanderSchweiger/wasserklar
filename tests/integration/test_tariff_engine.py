"""Tarif-Engine: Tarifpositionen + individuelle Gebühren (app/invoices/tariff_engine.py)."""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.invoices import tariff_engine as engine
from app.invoices.charges import (apply_override_form, build_tariff, charge_type,
                                  ensure_system_charge_types, tariff_form_params)
from app.models import ChargeOverride, ChargeType, Customer, Property, TariffComponent


@pytest.fixture
def setup(app):
    ensure_system_charge_types()
    tariff = build_tariff(name="T", valid_from=2026, water_price=Decimal("1.85"),
                          base_fee=Decimal("60"), water_levy=Decimal("0.10"),
                          tax_rate=Decimal("7"))
    cust = Customer(name="Kunde")
    prop = Property(object_type="Haus")
    db.session.add_all([cust, prop])
    db.session.commit()
    return {"tariff": tariff, "cust": cust, "prop": prop}


def _override(owner, key, amount):
    ov = ChargeOverride(charge_type_id=charge_type(key).id, amount=amount)
    owner.charge_overrides.append(ov)
    db.session.commit()
    return ov


def _by_key(charges):
    return {c.key: c for c in charges}


class TestResolveCharges:
    def test_tariff_values_without_overrides(self, setup):
        charges = _by_key(engine.resolve_charges(setup["tariff"]))
        assert charges["water"].unit_price == Decimal("1.85")
        assert charges["water_levy"].unit_price == Decimal("0.10")
        assert charges["water_levy"].is_levy is True
        assert charges["base_fee"].unit_price == Decimal("60")
        # Zusatzgebuehr ohne Betrag -> keine Position
        assert "additional_fee" not in charges
        assert all(c.source == engine.SOURCE_TARIFF for c in charges.values())

    def test_property_beats_customer_beats_tariff(self, setup):
        _override(setup["cust"], "base_fee", Decimal("40"))
        c = _by_key(engine.resolve_charges(setup["tariff"], prop=setup["prop"],
                                           customer=setup["cust"]))
        assert c["base_fee"].unit_price == Decimal("40")
        assert c["base_fee"].source == engine.SOURCE_CUSTOMER
        _override(setup["prop"], "base_fee", Decimal("25"))
        c = _by_key(engine.resolve_charges(setup["tariff"], prop=setup["prop"],
                                           customer=setup["cust"]))
        assert c["base_fee"].unit_price == Decimal("25")
        assert c["base_fee"].source == engine.SOURCE_PROPERTY

    def test_exempt_override_drops_position(self, setup):
        _override(setup["prop"], "base_fee", None)
        c = _by_key(engine.resolve_charges(setup["tariff"], prop=setup["prop"]))
        assert "base_fee" not in c

    def test_override_activates_component_without_amount(self, setup):
        """Tarifposition ohne Betrag = nur mit individuellem Betrag (bewahrt das
        fruehere Verhalten: Override erzeugt eine Gebuehr, die der Tarif nicht hat)."""
        _override(setup["cust"], "additional_fee", Decimal("12"))
        c = _by_key(engine.resolve_charges(setup["tariff"], customer=setup["cust"]))
        assert c["additional_fee"].unit_price == Decimal("12")
        assert c["additional_fee"].tax_rate == Decimal("7")   # USt aus dem Tarif

    def test_non_overridable_ignores_override(self, setup):
        _override(setup["prop"], "water_levy", Decimal("0"))
        c = _by_key(engine.resolve_charges(setup["tariff"], prop=setup["prop"]))
        assert c["water_levy"].unit_price == Decimal("0.10")

    def test_water_never_overridable(self, setup):
        water = charge_type("water")
        water.overridable = True     # z.B. per DB manipuliert
        db.session.commit()
        _override(setup["prop"], "water", None)
        c = _by_key(engine.resolve_charges(setup["tariff"], prop=setup["prop"]))
        assert c["water"].unit_price == Decimal("1.85")

    def test_ignored_override_types(self, setup):
        # Tarif ohne Zusatzgebuehr-Position
        tariff = build_tariff(name="Klein", valid_from=2026, water_price=Decimal("1"),
                              include_empty_fees=False)
        _override(setup["cust"], "additional_fee", Decimal("12"))
        assert engine.ignored_override_types(tariff, customer=setup["cust"]) == ["Zusatzgebühr"]

    def test_tax_only_when_vat_liable(self, setup):
        c = _by_key(engine.resolve_charges(setup["tariff"]))
        assert c["water"].tax(True) == Decimal("7")
        assert c["water"].tax(False) is None


class TestLines:
    def _levy(self, valid_from=None):
        return engine.Charge(key="water_levy", label="Wasserentnahmeentgelt",
                             calc_type=ChargeType.CALC_PER_M3,
                             unit_price=Decimal("0.10"), valid_from=valid_from)

    def test_per_m3_line_full_period(self):
        line = engine.per_m3_line(self._levy(), Decimal("120"),
                                  start=date(2026, 1, 1), end=date(2026, 12, 31),
                                  period_name="2026")
        assert line["quantity"] == Decimal("120")
        assert line["amount"] == Decimal("12.00")
        assert line["charge_key"] == "water_levy"
        assert line["description"] == ("Wasserentnahmeentgelt 2026 (120 m³ × 0,1000 €/m³)")

    def test_per_m3_line_apportioned_from_valid_from(self):
        """Bayern-Wassercent ab 1.7.2026: 184 von 365 Tagen."""
        line = engine.per_m3_line(self._levy(date(2026, 7, 1)), Decimal("365"),
                                  start=date(2026, 1, 1), end=date(2026, 12, 31))
        assert line["quantity"] == Decimal("184.000")
        assert line["amount"] == Decimal("18.40")
        assert "zeitanteilig ab 01.07.2026: 184/365 Tage" in line["description"]

    def test_per_m3_line_not_yet_valid(self):
        assert engine.per_m3_line(self._levy(date(2027, 1, 1)), Decimal("100"),
                                  start=date(2026, 1, 1), end=date(2026, 12, 31)) is None

    def test_flat_line_pro_rata(self):
        fee = engine.Charge(key="base_fee", label="Grundgebühr",
                            calc_type=ChargeType.CALC_FLAT, unit_price=Decimal("73"))
        line = engine.flat_line(fee, start=date(2026, 7, 2), end=date(2026, 12, 31),
                                period_days=365)
        assert line["amount"] == Decimal("36.60")
        assert line["description"] == "Grundgebühr (anteilig 183/365 Tage)"
        full = engine.flat_line(fee)
        assert full["amount"] == Decimal("73.00") and full["description"] == "Grundgebühr"


class TestOverrideForm:
    def test_apply_modes(self, setup):
        base = charge_type("base_fee").id
        add = charge_type("additional_fee").id
        cust = setup["cust"]
        err = apply_override_form(cust, {f"ov_mode_{base}": "amount",
                                         f"ov_amount_{base}": "45,50",
                                         f"ov_mode_{add}": "exempt"})
        assert err is None
        db.session.commit()
        rows = {o.charge_type_id: o for o in cust.charge_overrides}
        assert rows[base].amount == Decimal("45.50")
        assert rows[add].amount is None and rows[add].is_exempt
        # „wie Tarif" entfernt den Override wieder
        apply_override_form(cust, {f"ov_mode_{base}": "", f"ov_amount_{base}": ""})
        db.session.commit()
        assert {o.charge_type_id for o in cust.charge_overrides} == {add}

    def test_amount_typed_with_inherit_counts_as_amount(self, setup):
        base = charge_type("base_fee").id
        apply_override_form(setup["prop"], {f"ov_mode_{base}": "", f"ov_amount_{base}": "10"})
        db.session.commit()
        assert setup["prop"].charge_overrides[0].amount == Decimal("10")

    def test_invalid_amount_changes_nothing(self, setup):
        base = charge_type("base_fee").id
        add = charge_type("additional_fee").id
        err = apply_override_form(setup["cust"], {f"ov_mode_{add}": "exempt",
                                                  f"ov_mode_{base}": "amount",
                                                  f"ov_amount_{base}": "abc"})
        assert "gültigen Betrag" in err
        assert setup["cust"].charge_overrides == []


class TestTariffFormParams:
    def test_copy_keeps_components_and_replaces_amounts(self, setup):
        params = tariff_form_params(setup["tariff"], name="Neu", valid_from=2027,
                                    amounts={"water": Decimal("2.05")})
        water = charge_type("water").id
        levy = charge_type("water_levy").id
        assert params["name"] == "Neu" and params["valid_from"] == 2027
        assert params[f"comp_amount_{water}"] == "2,0500"
        assert params[f"comp_amount_{levy}"] == "0,1000"
        assert params[f"comp_tax_{levy}"] == "7"

    def test_component_unique_per_tariff(self, setup):
        from sqlalchemy.exc import IntegrityError
        db.session.add(TariffComponent(tariff_id=setup["tariff"].id,
                                       charge_type_id=charge_type("water").id,
                                       label="doppelt", amount=Decimal("1")))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
