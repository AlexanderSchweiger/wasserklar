"""Staffeln + Bedingungen in den Folgeprozessen: Schlussrechnung beim
Eigentuemerwechsel, Schaetz-Korrektur, Plankostenrechnung, Daten-Export/-Import.

Tarif wie in tests/http/test_tariff_tiers.py: Wasser bis 200 m³ 1,20 €, ueber
200 bis 500 m³ 1,50 €, ueber 500 m³ 2,00 €; Grundgebuehr 50 € nur Mitglieder.
"""
import io
import json
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.invoices import tariff_engine as engine
from app.invoices.charges import build_tariff
from app.meters.services import save_reading
from app.models import (
    BillingPeriod, BillingRun, Customer, FiscalYear, Invoice, InvoiceItem, MeterReading,
    Property, PropertyOwnership, ReadingCorrection, WaterMeter, WaterTariff,
)
from app.owner_change import services as oc_svc

TODAY = date.today()
D = Decimal


def _tariff(**kw):
    return build_tariff(
        name="Staffel", valid_from=TODAY.year, water_price=D("1.20"), base_fee=D("50"),
        tax_rate=D("10"), tiers={"water": [("200", "1.50"), ("500", "2.00")]},
        conditions={"base_fee": ["member"]}, **kw)


@pytest.fixture
def owner_setup(app, user):
    db.session.add(FiscalYear(year=TODAY.year, start_date=date(TODAY.year, 1, 1),
                              end_date=date(TODAY.year, 12, 31), is_vat_liable=False))
    period = BillingPeriod(name="P", start_date=date(TODAY.year, 1, 1),
                           end_date=date(TODAY.year, 12, 31), active=True)
    db.session.add(period)
    tariff = _tariff()
    old = Customer(name="Alt", customer_number=1)
    new = Customer(name="Neu", customer_number=2)
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add_all([old, new, prop])
    db.session.flush()
    db.session.add(PropertyOwnership(property_id=prop.id, customer_id=old.id,
                                     valid_from=date(TODAY.year - 3, 1, 1)))
    meter = WaterMeter(property_id=prop.id, meter_number="Z-1", meter_type="main",
                       initial_value=D("0"))
    db.session.add(meter)
    db.session.commit()
    return {"period": period, "tariff": tariff, "old": old, "new": new,
            "prop": prop, "meter": meter, "user": user}


def _change(s, value, *, resign=False):
    return oc_svc.execute_owner_change(
        prop=s["prop"], period=s["period"], stichtag=date(TODAY.year, 7, 1),
        new_customer_ids=[s["new"].id],
        meter_inputs={s["meter"].id: {"value": D(value), "is_estimated": False}},
        create_settlement=True, settlement_recipient_id=s["old"].id,
        tariff=s["tariff"], due_days=30, fee_mode=oc_svc.FEE_MODE_PRO_RATA,
        resign_customer_ids=[s["old"].id] if resign else None,
        created_by_id=s["user"].id)


class TestSettlement:
    def test_full_tiers_on_settlement_quantity(self, owner_setup):
        oc, _ = _change(owner_setup, "300")
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        water = [i for i in inv.items if i.charge_key == "water"]
        # Volle Grenzen je Rechnung: 200 × 1,20 + 100 × 1,50
        assert [(i.quantity, i.amount) for i in water] == [(D("200"), D("240.00")),
                                                           (D("100"), D("150.00"))]
        assert "Zähler Z-1: 300 m³" in water[0].description
        assert inv.consumption == D("300")

    def test_member_status_before_resignation_counts(self, owner_setup):
        """Der Assistent stellt den Altbesitzer erst NACH dem Rechnungsbau auf
        „ausgeschieden" — die Grundgebuehr (nur Mitglieder) bleibt anteilig drin."""
        oc, _ = _change(owner_setup, "100", resign=True)
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        base = [i for i in inv.items if i.charge_key == "base_fee"]
        assert len(base) == 1 and "anteilig" in base[0].description
        assert owner_setup["old"].wg_status == "resigned"

    def test_preview_matches_invoice(self, owner_setup):
        s = owner_setup
        preview = oc_svc.build_settlement_preview(
            prop=s["prop"], period=s["period"], stichtag=date(TODAY.year, 7, 1),
            tariff=s["tariff"], fee_mode=oc_svc.FEE_MODE_PRO_RATA, recipient=s["old"],
            meter_inputs={s["meter"].id: {"value": D("300"), "is_estimated": False}})
        oc, _ = _change(s, "300")
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        assert preview["gross_total"] == inv.total_amount


class TestEstimationCorrection:
    def _billed(self, app):
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
        tariff = _tariff()
        db.session.flush()
        save_reading(meter, period, D("450"), is_estimated=True,
                     reading_date=date(2025, 12, 1))
        run = BillingRun(billing_period_id=period.id, tariff_name=tariff.name,
                         tariff_price_per_m3=D("1.20"),
                         tariff_snapshot=json.dumps(engine.snapshot(tariff)))
        db.session.add(run)
        db.session.flush()
        inv = Invoice(invoice_number="2025-00001", customer_id=cust.id, property_id=prop.id,
                      billing_period_id=period.id, billing_run_id=run.id,
                      date=date.today(), status=Invoice.STATUS_SENT)
        db.session.add(inv)
        db.session.flush()
        charges = engine.resolve_charges(tariff, customer=cust)
        for line in engine.build_lines(charges, engine.BillingCase(
                consumption=D("450"), vat_liable=False, is_estimated=True)):
            inv.items.append(InvoiceItem(**line))
        db.session.commit()
        return meter, period, inv

    def test_correction_follows_tiers(self, app):
        meter, period, inv = self._billed(app)
        assert [i.amount for i in inv.items if i.charge_key == "water"] == [
            D("240.00"), D("375.00")]
        save_reading(meter, period, D("550"), is_estimated=False,
                     reading_date=date(2025, 12, 15))
        db.session.commit()
        corr = ReadingCorrection.query.filter_by(charge_key="water").one()
        # A(550) − A(450) = (240 + 450 + 100) − (240 + 375) = 175 — nicht 100 × 1,50
        assert corr.amount == D("175.00")
        assert corr.unit_price == D("1.75")

    def test_credit_follows_tiers(self, app):
        meter, period, inv = self._billed(app)
        save_reading(meter, period, D("150"), is_estimated=False,
                     reading_date=date(2025, 12, 15))
        db.session.commit()
        corr = ReadingCorrection.query.filter_by(charge_key="water").one()
        # A(150) − A(450) = 180 − 615
        assert corr.amount == D("-435.00")


class TestCostPlanning:
    def _setup(self):
        from app.models import AppSetting
        AppSetting.set("org.type", "cooperative")
        period = BillingPeriod(name="Vorjahr", start_date=date(TODAY.year - 1, 1, 1),
                               end_date=date(TODAY.year - 1, 12, 31), active=True)
        db.session.add(period)
        tariff = _tariff()
        db.session.flush()
        for n, (cons, status) in enumerate([("100", None), ("600", "external")], start=1):
            cust = Customer(name=f"K{n}", customer_number=n)
            prop = Property(object_number=f"P-{n}", object_type="Haus")
            db.session.add_all([cust, prop])
            db.session.flush()
            if status:
                cust.ensure_wg_profile().status = status
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

    def test_effective_price_and_condition_units(self, app):
        from app.cost_planning import services as cp
        tariff = self._setup()
        info = cp.effective_water_price(tariff)
        # 100 × 1,20 + (200 × 1,20 + 300 × 1,50 + 100 × 2,00) = 1010 / 700 m³
        assert info["tiered"] and info["price"] == D("1.4429")
        base = cp.baseline(tariff)
        assert base["water_tiered"] and base["effective_price"] == D("1.4429")
        assert base["base_fee_units"] == 1          # nur das Mitglied zahlt die Grundgebuehr

    def test_scenario_shift_is_exact(self, app):
        from app.cost_planning import services as cp
        tariff = self._setup()
        water = engine.water_charge(engine.resolve_charges(tariff))
        shifted = engine.make_charge(
            key="water", label="W", calc_type=water.calc_type, amount=D("1.30"),
            tiers=[type(s)(s.above, s.price + D("0.10")) for s in water.tiers])
        for qty in (D("100"), D("600")):
            delta = engine.volume_amount(shifted, qty) - engine.volume_amount(water, qty)
            assert delta == (qty * D("0.10")).quantize(D("0.01"))
        assert cp.effective_water_price(None)["price"] is None


class TestDataTransfer:
    def test_roundtrip_and_old_export(self, app, tmp_path, monkeypatch):
        from app.data_transfer.services import (export_to_zip, extract_to_temp,
                                                import_from_zip, validate_manifest)
        pdf_dir = tmp_path / "t" / "pdfs"
        pdf_dir.mkdir(parents=True)
        monkeypatch.setitem(app.config, "PDF_DIR", str(pdf_dir))
        instance = tmp_path / "instance"
        instance.mkdir()
        tariff = _tariff()
        db.session.commit()
        tariff_id = tariff.id

        buf = io.BytesIO()
        export_to_zip({"stammdaten": True, "buchungen": False, "mahnwesen": False,
                       "einstellungen": False, "years": [], "include_pdfs": False},
                      buf, exported_by="test")

        def do_import(zip_bytes):
            extract_dir, manifest = extract_to_temp(io.BytesIO(zip_bytes), str(instance))
            assert validate_manifest(manifest, extract_dir)["errors"] == []
            import_from_zip(extract_dir, manifest, mode="replace", instance_path=str(instance))
            db.session.remove()

        do_import(buf.getvalue())
        water = db.session.get(WaterTariff, tariff_id).water_component
        assert [s.above for s in water.tier_steps] == [D("200"), D("500")]
        assert db.session.get(WaterTariff, tariff_id).component(
            "base_fee").contact_statuses == {"member"}

        # Export einer Vorversion ohne die neuen Spalten → Standardwerte
        from tests.integration.test_document_transfer import tamper

        def strip(records):
            for r in records:
                for key in ("tier_mode", "tiers", "conditions"):
                    r.pop(key, None)

        do_import(tamper(buf.getvalue(), "tariff_components", strip))
        comp = db.session.get(WaterTariff, tariff_id).water_component
        assert comp.tier_mode == "graduated" and comp.tiers is None and comp.conditions is None

