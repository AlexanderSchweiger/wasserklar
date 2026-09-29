"""Wassercent (weitere m³-Position) in den Folgeprozessen: Schlussrechnung beim
Eigentuemerwechsel, Schaetz-Korrektur, Plankostenrechnung, Legacy-Import."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.invoices.charges import build_tariff
from app.meters.services import save_reading
from app.models import (
    BillingPeriod, ChargeOverride, ChargeType, Customer, FiscalYear, Invoice,
    InvoiceItem, Property, PropertyOwnership, ReadingCorrection, TariffComponent,
    WaterMeter, WaterTariff,
)
from app.owner_change import services as oc_svc

TODAY = date.today()


@pytest.fixture
def owner_setup(app, user):
    db.session.add(FiscalYear(year=TODAY.year, start_date=date(TODAY.year, 1, 1),
                              end_date=date(TODAY.year, 12, 31), is_vat_liable=True))
    period = BillingPeriod(name="P", start_date=date(TODAY.year, 1, 1),
                           end_date=date(TODAY.year, 12, 31), active=True)
    db.session.add(period)
    tariff = build_tariff(name="T", valid_from=TODAY.year, water_price=Decimal("2"),
                          water_levy=Decimal("0.10"), base_fee=Decimal("36.50"),
                          tax_rate=Decimal("7"))
    old = Customer(name="Alt", customer_number=1)
    new = Customer(name="Neu", customer_number=2)
    db.session.add_all([old, new])
    db.session.flush()
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    db.session.add(PropertyOwnership(property_id=prop.id, customer_id=old.id,
                                     valid_from=date(TODAY.year - 3, 1, 1)))
    meter = WaterMeter(property_id=prop.id, meter_number="Z-1", meter_type="main",
                       initial_value=Decimal("100"))
    db.session.add(meter)
    db.session.commit()
    return {"period": period, "tariff": tariff, "old": old, "new": new,
            "prop": prop, "meter": meter, "user": user}


def _change(s, fee_mode):
    return oc_svc.execute_owner_change(
        prop=s["prop"], period=s["period"], stichtag=date(TODAY.year, 7, 1),
        new_customer_ids=[s["new"].id],
        meter_inputs={s["meter"].id: {"value": Decimal("130"), "is_estimated": False}},
        create_settlement=True, settlement_recipient_id=s["old"].id,
        tariff=s["tariff"], due_days=30, fee_mode=fee_mode,
        created_by_id=s["user"].id)


class TestSettlement:
    def test_levy_on_settlement_invoice(self, owner_setup):
        oc, _ = _change(owner_setup, oc_svc.FEE_MODE_PRO_RATA)
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        items = {i.charge_key: i for i in inv.items}
        assert items["water"].quantity == Decimal("30")
        assert items["water_levy"].quantity == Decimal("30")
        assert items["water_levy"].amount == Decimal("3.00")
        assert items["water_levy"].tax_rate == Decimal("7")
        assert "base_fee" in items and "anteilig" in items["base_fee"].description
        assert inv.consumption == Decimal("30")

    def test_levy_on_settlement_respects_customer_override(self, owner_setup):
        base = ChargeType.query.filter_by(key="base_fee").one()
        owner_setup["old"].charge_overrides.append(
            ChargeOverride(charge_type_id=base.id, amount=None))   # entfällt
        db.session.commit()
        oc, _ = _change(owner_setup, oc_svc.FEE_MODE_PRO_RATA)
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        assert "base_fee" not in {i.charge_key for i in inv.items}

    def test_deductions_carry_change_date(self, owner_setup):
        _change(owner_setup, oc_svc.FEE_MODE_NEW_OWNER_FULL)
        ded = oc_svc.deductions_for_property(owner_setup["prop"].id,
                                             owner_setup["period"].id)
        assert ded["change_date"] == date(TODAY.year, 7, 1)


class TestEstimationCorrection:
    def test_levy_gets_own_correction(self, app):
        period = BillingPeriod(name="2025", start_date=date(2025, 1, 1),
                               end_date=date(2025, 12, 31), active=True)
        prop = Property(object_number="P-1", object_type="Haus")
        cust = Customer(name="Kunde")
        db.session.add_all([period, prop, cust])
        db.session.flush()
        db.session.add(PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                                         valid_from=date(2000, 1, 1)))
        meter = WaterMeter(property_id=prop.id, meter_number="Z1",
                           initial_value=Decimal("0"), active=True, meter_type="main")
        db.session.add(meter)
        db.session.flush()
        from app.invoices.charges import ensure_system_charge_types
        ensure_system_charge_types()
        save_reading(meter, period, Decimal("50"), is_estimated=True,
                     reading_date=date(2025, 12, 1))
        inv = Invoice(invoice_number="2025-00001", customer_id=cust.id,
                      property_id=prop.id, billing_period_id=period.id,
                      date=date.today(), status=Invoice.STATUS_SENT)
        db.session.add(inv)
        db.session.flush()
        db.session.add_all([
            InvoiceItem(invoice_id=inv.id, description="Wasserverbrauch 2025",
                        quantity=Decimal("50"), unit="m³", unit_price=Decimal("2"),
                        amount=Decimal("100.00"), tax_rate=Decimal("7"),
                        charge_key="water", is_estimated=True),
            InvoiceItem(invoice_id=inv.id, description="Wasserentnahmeentgelt 2025",
                        quantity=Decimal("50"), unit="m³", unit_price=Decimal("0.10"),
                        amount=Decimal("5.00"), tax_rate=Decimal("7"),
                        charge_key="water_levy", is_estimated=True),
        ])
        db.session.commit()

        save_reading(meter, period, Decimal("60"), is_estimated=False,
                     reading_date=date(2025, 12, 15))
        db.session.commit()
        corrs = {c.charge_key: c for c in ReadingCorrection.query.all()}
        assert corrs["water"].amount == Decimal("20.00")
        assert corrs["water_levy"].amount == Decimal("1.00")
        assert corrs["water_levy"].label == "Wasserentnahmeentgelt"
        assert corrs["water_levy"].tax_rate == Decimal("7")


class TestCostPlanning:
    def test_levy_is_not_revenue(self, app):
        from app.cost_planning import services as cp
        tariff = build_tariff(name="T", valid_from=2025, water_price=Decimal("1.50"),
                              water_levy=Decimal("0.10"), base_fee=Decimal("50"))
        db.session.commit()
        info = cp.baseline(tariff)
        scs = cp.build_scenarios(Decimal("1000"), info)
        volume = next(s for s in scs if s.key == "volume")
        # Wasserpreis ist der Hebel — der Wassercent bleibt aussen vor.
        assert volume.price_per_m3 >= Decimal("1.5000")
        assert info["current_volume_revenue"] == Decimal("0.00")  # keine Verbrauchsdaten


class TestLegacyImport:
    def test_old_export_is_translated(self, app):
        """Ein Export vor v1.44.0 (Tarif-Spalten) wird in Positionen/Overrides
        uebersetzt — sonst gingen Gebuehren beim Re-Import still verloren."""
        from app.data_transfer.services import _upgrade_legacy_tariff_records
        from app.data_transfer.registry import CATEGORIES
        table_records = {
            WaterTariff: [{"id": 7, "name": "Alt", "valid_from": 2020,
                           "price_per_m3": "1.2000", "base_fee": "30.00",
                           "base_fee_label": "Grundgebühr", "additional_fee": None,
                           "additional_fee_label": "Zählermiete",
                           "price_per_m3_account_id": 3}],
            Customer: [{"id": 5, "name": "K", "base_fee_override": "20.00",
                        "additional_fee_override": None}],
            Property: [{"id": 9, "object_type": "Haus", "base_fee_override": None,
                        "additional_fee_override": "4.00"}],
        }
        models = CATEGORIES["stammdaten"]
        _upgrade_legacy_tariff_records(table_records, models)
        cts = {r["key"]: r["id"] for r in table_records[ChargeType]}
        comps = {(r["charge_type_id"]): r for r in table_records[TariffComponent]}
        assert comps[cts["water"]]["amount"] == "1.2000"
        assert comps[cts["water"]]["account_id"] == 3
        assert comps[cts["base_fee"]]["amount"] == "30.00"
        assert comps[cts["additional_fee"]]["label"] == "Zählermiete"
        assert comps[cts["additional_fee"]]["amount"] is None
        ovs = table_records[ChargeOverride]
        assert {(o["charge_type_id"], o["customer_id"], o["property_id"], o["amount"])
                for o in ovs} == {(cts["base_fee"], 5, None, "20.00"),
                                  (cts["additional_fee"], None, 9, "4.00")}

    def test_current_export_untouched(self, app):
        from app.data_transfer.services import _upgrade_legacy_tariff_records
        from app.data_transfer.registry import CATEGORIES
        table_records = {WaterTariff: [{"id": 1, "name": "Neu", "valid_from": 2026}]}
        _upgrade_legacy_tariff_records(table_records, CATEGORIES["stammdaten"])
        assert TariffComponent not in table_records
