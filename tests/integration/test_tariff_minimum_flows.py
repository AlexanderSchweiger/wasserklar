"""Mindestabnahme in den Folgeprozessen: Schlussrechnung + Folgelauf beim
Eigentuemerwechsel, Schaetz-Korrektur, Gutschrift, Plankostenrechnung,
individuelle Mindestabnahme (Formular-Service, Engine), Daten-Export/-Import.

Tarif: Wasser 1,40 €/m³ mit 50 m³ Mindestabnahme, Grundgebuehr 36,50 €.
"""
import io
import json
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.invoices import tariff_engine as engine
from app.invoices.charges import apply_override_form, build_tariff, charge_type
from app.meters.services import save_reading
from app.models import (
    BillingPeriod, BillingRun, Customer, FiscalYear, Invoice, InvoiceItem,
    MinimumOverride, Property, PropertyOwnership, ReadingCorrection, WaterMeter,
    WaterTariff,
)
from app.owner_change import services as oc_svc

TODAY = date.today()
D = Decimal


def _tariff(minimum="50", base_fee=D("36.50"), **kw):
    return build_tariff(name="Mindest", valid_from=TODAY.year, water_price=D("1.40"),
                        base_fee=base_fee, tax_rate=None,
                        minimums={"water": minimum} if minimum else None, **kw)


def _fills(inv):
    return [(i.quantity, i.amount) for i in inv.items if i.is_minimum_fill]


# ---------------------------------------------------------------------------
# Eigentuemerwechsel
# ---------------------------------------------------------------------------

@pytest.fixture
def owner_setup(app, user):
    year = TODAY.year
    db.session.add(FiscalYear(year=year, start_date=date(year, 1, 1),
                              end_date=date(year, 12, 31), is_vat_liable=False))
    period = BillingPeriod(name="P", start_date=date(year, 1, 1),
                           end_date=date(year, 12, 31), active=True)
    db.session.add(period)
    tariff = _tariff()
    old = Customer(name="Alt", customer_number=1)
    new = Customer(name="Neu", customer_number=2)
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add_all([old, new, prop])
    db.session.flush()
    db.session.add(PropertyOwnership(property_id=prop.id, customer_id=old.id,
                                     valid_from=date(year - 3, 1, 1)))
    meter = WaterMeter(property_id=prop.id, meter_number="Z-1", meter_type="main",
                       initial_value=D("0"))
    db.session.add(meter)
    db.session.commit()
    return {"period": period, "tariff": tariff, "old": old, "new": new,
            "prop": prop, "meter": meter, "user": user}


def _change(s, value, fee_mode):
    return oc_svc.execute_owner_change(
        prop=s["prop"], period=s["period"], stichtag=date(TODAY.year, 7, 1),
        new_customer_ids=[s["new"].id],
        meter_inputs={s["meter"].id: {"value": D(value), "is_estimated": False}},
        create_settlement=True, settlement_recipient_id=s["old"].id,
        tariff=s["tariff"], due_days=30, fee_mode=fee_mode,
        created_by_id=s["user"].id)


class TestOwnerChange:
    def test_pro_rata_settlement_prorates_minimum(self, owner_setup):
        oc, _ = _change(owner_setup, "10", oc_svc.FEE_MODE_PRO_RATA)
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        old_days, _, period_days = oc_svc.fee_day_split(owner_setup["period"],
                                                         date(TODAY.year, 7, 1))
        part = (D("50") * old_days / period_days).quantize(D("0.001"))
        [fill] = [i for i in inv.items if i.is_minimum_fill]
        assert fill.quantity == part - D("10")
        assert fill.min_quantity == part
        assert f"anteilig {old_days}/{period_days} Tage von 50 m³" in fill.description
        assert inv.consumption == D("10")

    def test_new_owner_full_settlement_without_minimum(self, owner_setup):
        oc, _ = _change(owner_setup, "10", oc_svc.FEE_MODE_NEW_OWNER_FULL)
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        assert _fills(inv) == []
        assert all(i.min_quantity is None for i in inv.items)

    def test_preview_matches_invoice(self, owner_setup):
        s = owner_setup
        preview = oc_svc.build_settlement_preview(
            prop=s["prop"], period=s["period"], stichtag=date(TODAY.year, 7, 1),
            tariff=s["tariff"], fee_mode=oc_svc.FEE_MODE_PRO_RATA, recipient=s["old"],
            meter_inputs={s["meter"].id: {"value": D("10"), "is_estimated": False}})
        assert any(l.get("is_minimum_fill") for l in preview["lines"])
        oc, _ = _change(s, "10", oc_svc.FEE_MODE_PRO_RATA)
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        assert preview["gross_total"] == inv.total_amount

    @pytest.mark.parametrize("fee_mode", [oc_svc.FEE_MODE_PRO_RATA,
                                          oc_svc.FEE_MODE_NEW_OWNER_FULL])
    def test_follow_up_run_gets_rest(self, owner_setup, logged_in_admin, fee_mode):
        s = owner_setup
        _change(s, "10", fee_mode)
        save_reading(s["meter"], s["period"], D("30"), is_estimated=False,
                     reading_date=date(TODAY.year, 12, 31))
        db.session.commit()
        resp = logged_in_admin.post("/invoices/generate", data={
            "billing_period_id": str(s["period"].id), "tariff_id": str(s["tariff"].id),
            "due_days": "30"})
        assert resp.status_code == 302
        inv = Invoice.query.filter_by(customer_id=s["new"].id,
                                      invoice_kind=Invoice.KIND_STANDARD).one()
        assert inv.consumption == D("20")
        [fill] = [i for i in inv.items if i.is_minimum_fill]
        if fee_mode == oc_svc.FEE_MODE_PRO_RATA:
            _, new_days, period_days = oc_svc.fee_day_split(s["period"], date(TODAY.year, 7, 1))
            part = (D("50") * new_days / period_days).quantize(D("0.001"))
            assert fill.min_quantity == part and fill.quantity == part - D("20")
        else:
            assert fill.min_quantity == D("50") and fill.quantity == D("30")


@pytest.fixture
def logged_in_admin(client, user):
    from tests.conftest import _ensure_role
    from app.models import User
    role = _ensure_role("Admin")
    u = User(username="mini", email="mini@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "mini", "password": "secret"})
    return client


# ---------------------------------------------------------------------------
# Schaetz-Korrektur
# ---------------------------------------------------------------------------

class TestEstimationCorrection:
    def _billed(self, estimate, *, tariff=None, minimum_override=None):
        period = BillingPeriod(name="2025", start_date=date(2025, 1, 1),
                               end_date=date(2025, 12, 31), active=True)
        prop = Property(object_number="P-1", object_type="Haus")
        cust = Customer(name="Kunde")
        db.session.add_all([period, prop, cust])
        db.session.flush()
        db.session.add(PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                                         valid_from=date(2000, 1, 1)))
        meter = WaterMeter(property_id=prop.id, meter_number="Z1",
                           initial_value=D("0"), active=True, meter_type="main")
        db.session.add(meter)
        tariff = tariff or _tariff(base_fee=None)
        if minimum_override is not None:
            db.session.add(MinimumOverride(charge_type_id=charge_type("water").id,
                                           property_id=prop.id, min_quantity=minimum_override))
        db.session.flush()
        save_reading(meter, period, D(estimate), is_estimated=True,
                     reading_date=date(2025, 12, 1))
        run = BillingRun(billing_period_id=period.id, tariff_name=tariff.name,
                         tariff_price_per_m3=D("1.40"),
                         tariff_snapshot=json.dumps(engine.snapshot(tariff)))
        db.session.add(run)
        db.session.flush()
        inv = Invoice(invoice_number="2025-00001", customer_id=cust.id, property_id=prop.id,
                      billing_period_id=period.id, billing_run_id=run.id,
                      date=date.today(), status=Invoice.STATUS_SENT)
        db.session.add(inv)
        db.session.flush()
        charges = engine.resolve_charges(tariff, prop=prop, customer=cust)
        for line in engine.build_lines(charges, engine.BillingCase(
                consumption=D(estimate), vat_liable=False, is_estimated=True)):
            inv.items.append(InvoiceItem(**line))
        db.session.commit()
        return meter, period, inv

    def _correct(self, meter, period, real):
        save_reading(meter, period, D(real), is_estimated=False,
                     reading_date=date(2025, 12, 15))
        db.session.commit()
        return ReadingCorrection.query.filter_by(charge_key="water").all()

    def test_change_below_minimum_costs_nothing(self, app):
        meter, period, _ = self._billed("40")
        assert self._correct(meter, period, "45") == []

    def test_credit_stops_at_minimum(self, app):
        meter, period, _ = self._billed("60")
        [corr] = self._correct(meter, period, "40")
        assert corr.amount == D("-14.00")            # nur 10 m³ (60 → 50)
        assert corr.delta_m3 == D("-20")             # Zaehlerdifferenz bleibt Fakt
        assert corr.min_quantity == D("50")

    def test_increase_above_minimum(self, app):
        meter, period, _ = self._billed("40")
        [corr] = self._correct(meter, period, "70")
        assert corr.amount == D("28.00")             # 50 → 70
        assert corr.min_quantity == D("50")

    def test_without_effect_no_note(self, app):
        meter, period, _ = self._billed("60")
        [corr] = self._correct(meter, period, "80")
        assert corr.amount == D("28.00") and corr.min_quantity is None

    def test_tiers_and_minimum(self, app):
        tariff = _tariff(minimum="250", base_fee=None,
                         tiers={"water": [("200", "1.50")]})
        meter, period, inv = self._billed("100", tariff=tariff)
        # abgerechnet: 100 × 1,40 + Auffuellung 100 × 1,40 + 50 × 1,50
        assert sum(i.amount for i in inv.items) == D("355.00")
        [corr] = self._correct(meter, period, "300")
        # A(300) − A(250) = 50 × 1,50
        assert corr.amount == D("75.00")

    def test_individual_minimum_frozen(self, app):
        meter, period, _ = self._billed("60", minimum_override=D("80"))
        assert self._correct(meter, period, "70") == []

    def test_correction_text_mentions_minimum(self, app):
        from app.meters.estimation import apply_corrections_to_invoice
        meter, period, inv = self._billed("60")
        self._correct(meter, period, "40")
        nxt = Invoice(invoice_number="2026-00001", customer_id=inv.customer_id,
                      date=date.today(), status=Invoice.STATUS_DRAFT)
        db.session.add(nxt)
        db.session.flush()
        nxt.items.append(InvoiceItem(description="Grundgebühr", quantity=D("1"),
                                     unit="Pauschal", unit_price=D("100"), amount=D("100")))
        db.session.flush()
        apply_corrections_to_invoice(nxt, inv.customer_id)
        [line] = [i for i in nxt.items if i.reading_correction_id]
        assert "Mindestabnahme 50 m³ berücksichtigt" in line.description


# ---------------------------------------------------------------------------
# Gutschrift, Engine-Aufloesung, Formular-Service
# ---------------------------------------------------------------------------

class TestCreditNote:
    def test_copies_flags(self, app, user):
        from app.invoices.credit_note import create_credit_note
        tariff = _tariff(base_fee=None)
        cust = Customer(name="K")
        db.session.add(cust)
        db.session.flush()
        inv = Invoice(invoice_number=f"{TODAY.year}-00001", customer_id=cust.id,
                      date=TODAY, status=Invoice.STATUS_SENT)
        db.session.add(inv)
        db.session.flush()
        for line in engine.build_lines(engine.resolve_charges(tariff),
                                       engine.BillingCase(consumption=D("30"))):
            inv.items.append(InvoiceItem(**line))
        inv.recalculate_total()
        db.session.commit()
        credit = create_credit_note(inv, created_by_id=user.id)
        assert credit.consumption == D("-30")
        assert [i.is_minimum_fill for i in credit.items] == [False, True]


class TestResolve:
    def _owner(self):
        cust = Customer(name="K")
        prop = Property(object_number="P-9", object_type="Haus")
        db.session.add_all([cust, prop])
        db.session.flush()
        return cust, prop

    def test_priority_property_customer_tariff(self, app):
        tariff = _tariff()
        cust, prop = self._owner()
        water_id = charge_type("water").id
        water = lambda **kw: engine.water_charge(engine.resolve_charges(tariff, **kw))
        assert water(prop=prop, customer=cust).min_qty == D("50")
        db.session.add(MinimumOverride(charge_type_id=water_id, customer_id=cust.id,
                                       min_quantity=D("70")))
        db.session.flush()
        w = water(prop=prop, customer=cust)
        assert (w.min_qty, w.min_source) == (D("70"), engine.SOURCE_CUSTOMER)
        db.session.add(MinimumOverride(charge_type_id=water_id, property_id=prop.id,
                                       min_quantity=None))
        db.session.flush()
        w = water(prop=prop, customer=cust)
        assert (w.min_qty, w.min_source) == (None, engine.SOURCE_PROPERTY)
        # Ohne Objekt/Kunde (Tarifrechner, manuelle Tarifzeile): der Tarif.
        assert water().min_qty == D("50")

    def test_ignored_override_hint(self, app):
        tariff = _tariff()
        cust, prop = self._owner()
        levy = charge_type("water_levy")
        db.session.add(MinimumOverride(charge_type_id=levy.id, property_id=prop.id,
                                       min_quantity=D("10")))
        db.session.flush()
        labels = engine.ignored_override_types(tariff, prop=prop, customer=cust)
        assert f"{levy.label}: Mindestabnahme" in labels

    def test_override_form_service(self, app):
        from app.invoices.charges import ensure_system_charge_types
        ensure_system_charge_types()
        cust, _ = self._owner()
        water = charge_type("water")
        err = apply_override_form(cust, {f"min_mode_{water.id}": "amount",
                                         f"min_qty_{water.id}": "0"})
        assert "Mindestabnahme in m³" in err
        assert cust.minimum_overrides == []
        assert apply_override_form(cust, {f"min_mode_{water.id}": "",
                                          f"min_qty_{water.id}": "12,5"}) is None
        assert [m.min_quantity for m in cust.minimum_overrides] == [D("12.5")]
        # Ohne Feld im Formular bleibt der Stand unveraendert.
        assert apply_override_form(cust, {}) is None
        assert len(cust.minimum_overrides) == 1


# ---------------------------------------------------------------------------
# Plankostenrechnung
# ---------------------------------------------------------------------------

class TestCostPlanning:
    def _setup(self):
        period = BillingPeriod(name="Vorjahr", start_date=date(TODAY.year - 1, 1, 1),
                               end_date=date(TODAY.year - 1, 12, 31), active=True)
        db.session.add(period)
        tariff = _tariff()
        db.session.flush()
        from app.models import MeterReading
        for n, cons in enumerate(["20", "100", "0"], start=1):
            cust = Customer(name=f"K{n}", customer_number=n)
            prop = Property(object_number=f"P-{n}", object_type="Haus")
            db.session.add_all([cust, prop])
            db.session.flush()
            db.session.add(PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                                             valid_from=date(2020, 1, 1)))
            meter = WaterMeter(property_id=prop.id, meter_number=f"Z{n}", meter_type="main",
                               active=True)
            db.session.add(meter)
            db.session.flush()
            db.session.add(MeterReading(meter_id=meter.id, billing_period_id=period.id,
                                        value=D(cons), consumption=D(cons)))
        db.session.commit()
        return tariff

    def test_effective_price_includes_fill(self, app):
        from app.cost_planning import services as cp
        tariff = self._setup()
        info = cp.effective_water_price(tariff)
        # abgerechnet 50 + 100 + 50 = 200 m³ × 1,40 = 280 € / 120 m³ verbraucht
        assert info["minimum"] and not info["tiered"]
        assert info["price"] == D("2.3333")
        assert info["billed_factor"] == D("1.6667")

    def test_individual_exempt_counts(self, app):
        from app.cost_planning import services as cp
        tariff = self._setup()
        prop = Property.query.filter_by(object_number="P-3").one()
        db.session.add(MinimumOverride(charge_type_id=charge_type("water").id,
                                       property_id=prop.id, min_quantity=None))
        db.session.commit()
        info = cp.effective_water_price(tariff)
        # 50 + 100 + 0 = 150 m³ × 1,40 = 210 € / 120 m³
        assert info["price"] == D("1.7500")

    def test_without_minimum_unchanged(self, app):
        from app.cost_planning import services as cp
        tariff = _tariff(minimum=None)
        db.session.commit()
        info = cp.effective_water_price(tariff)
        assert info == {"price": D("1.40"), "tiered": False, "minimum": False,
                        "billed_factor": D("1"), "period": None}


# ---------------------------------------------------------------------------
# Daten-Export/-Import
# ---------------------------------------------------------------------------

class TestDataTransfer:
    def test_roundtrip(self, app, tmp_path, monkeypatch):
        from app.data_transfer.services import (export_to_zip, extract_to_temp,
                                                import_from_zip, validate_manifest)
        pdf_dir = tmp_path / "t" / "pdfs"
        pdf_dir.mkdir(parents=True)
        monkeypatch.setitem(app.config, "PDF_DIR", str(pdf_dir))
        instance = tmp_path / "instance"
        instance.mkdir()
        tariff = _tariff()
        cust = Customer(name="K", customer_number=1)
        db.session.add(cust)
        db.session.flush()
        db.session.add(MinimumOverride(charge_type_id=charge_type("water").id,
                                       customer_id=cust.id, min_quantity=D("75")))
        db.session.commit()
        tariff_id, cust_id = tariff.id, cust.id

        buf = io.BytesIO()
        export_to_zip({"stammdaten": True, "buchungen": False, "mahnwesen": False,
                       "einstellungen": False, "years": [], "include_pdfs": False},
                      buf, exported_by="test")
        extract_dir, manifest = extract_to_temp(io.BytesIO(buf.getvalue()), str(instance))
        assert validate_manifest(manifest, extract_dir)["errors"] == []
        import_from_zip(extract_dir, manifest, mode="replace", instance_path=str(instance))
        db.session.remove()

        assert db.session.get(WaterTariff, tariff_id).water_component.min_quantity == D("50")
        cust = db.session.get(Customer, cust_id)
        assert [m.min_quantity for m in cust.minimum_overrides] == [D("75")]
