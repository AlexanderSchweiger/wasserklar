"""Integration-Tests fuer den Eigentuemerwechsel-Service (DB-beruehrend)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.owner_change import services as svc
from app.models import (
    AppSetting, BillingPeriod, Customer, FiscalYear, Invoice, MeterReading,
    OwnerChange, OwnerChangeMeterValue, Property, PropertyOwnership,
    ReadingCorrection, WaterMeter, WaterTariff,
)
from app.meters.services import save_reading


TODAY = date.today()


@pytest.fixture
def setup(app, user):
    """Periode (enthaelt heute), Buchungsjahr, Tarif, Objekt mit Zaehler
    (initial_value) + Altbesitzer + neuer Kontakt."""
    db.session.add(FiscalYear(
        year=TODAY.year, start_date=date(TODAY.year, 1, 1),
        end_date=date(TODAY.year, 12, 31)))
    period = BillingPeriod(
        name="P", start_date=date(TODAY.year, 1, 1),
        end_date=date(TODAY.year, 12, 31), active=True)
    db.session.add(period)
    tariff = WaterTariff(
        name="T", valid_from=TODAY.year, base_fee=Decimal("36.50"),
        price_per_m3=Decimal("2"))
    db.session.add(tariff)
    old = Customer(name="Alt Besitzer", customer_number=1)
    new = Customer(name="Neu Besitzer", customer_number=2)
    db.session.add_all([old, new])
    db.session.flush()
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    db.session.add(PropertyOwnership(
        property_id=prop.id, customer_id=old.id,
        valid_from=date(TODAY.year - 3, 1, 1), valid_to=None))
    meter = WaterMeter(property_id=prop.id, meter_number="Z-1",
                       meter_type="main", initial_value=Decimal("100"))
    db.session.add(meter)
    db.session.commit()
    return {"period": period, "tariff": tariff, "old": old, "new": new,
            "prop": prop, "meter": meter, "user": user}


def _stichtag():
    # Mitte der Periode, sicher innerhalb.
    return date(TODAY.year, 7, 1)


def _run(setup, *, create_settlement=True, fee_mode=svc.FEE_MODE_NEW_OWNER_FULL,
         value=Decimal("130"), new_ids=None):
    return svc.execute_owner_change(
        prop=setup["prop"], period=setup["period"], stichtag=_stichtag(),
        new_customer_ids=new_ids or [setup["new"].id],
        meter_inputs={setup["meter"].id: {"value": value, "is_estimated": False}},
        create_settlement=create_settlement,
        settlement_recipient_id=setup["old"].id,
        tariff=setup["tariff"], due_days=30, fee_mode=fee_mode,
        created_by_id=setup["user"].id,
    )


class TestExecute:
    def test_ownership_transfer_and_snapshot(self, setup):
        oc, warnings = _run(setup)
        # Altbesitzer beendet zum Vortag, neuer aktiv ab Stichtag.
        old_ow = PropertyOwnership.query.filter_by(
            property_id=setup["prop"].id, customer_id=setup["old"].id).one()
        assert old_ow.valid_to == _stichtag() - timedelta(days=1)
        new_ow = PropertyOwnership.query.filter_by(
            property_id=setup["prop"].id, customer_id=setup["new"].id).one()
        assert new_ow.valid_from == _stichtag()
        assert new_ow.valid_to is None
        # Snapshot: 130 - 100 (initial_value) = 30 m³.
        mv = OwnerChangeMeterValue.query.filter_by(owner_change_id=oc.id).one()
        assert mv.value_at_change == Decimal("130")
        assert mv.consumption_billed == Decimal("30")

    def test_settlement_invoice_consumption_line(self, setup):
        oc, _ = _run(setup)
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        assert inv is not None
        assert inv.invoice_kind == Invoice.KIND_FINAL_SETTLEMENT
        assert inv.customer_id == setup["old"].id
        m3 = [i for i in inv.items if i.unit == "m³"]
        assert len(m3) == 1
        assert m3[0].quantity == Decimal("30")
        # new_owner_full -> keine Gebuehrenposition auf der Schlussrechnung.
        assert not [i for i in inv.items if i.unit == "Pauschal"]

    def test_pro_rata_adds_prorated_base_fee(self, setup):
        oc, _ = _run(setup, fee_mode=svc.FEE_MODE_PRO_RATA)
        assert oc.fee_days_billed == (date(TODAY.year, 7, 1) - date(TODAY.year, 1, 1)).days
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        fee_items = [i for i in inv.items if i.unit == "Pauschal"]
        assert len(fee_items) == 1
        # 36.50 * old_days/period_days, gerundet.
        period_days = (setup["period"].end_date - setup["period"].start_date).days + 1
        old_days = oc.fee_days_billed
        expected = (Decimal("36.50") * Decimal(old_days) / Decimal(period_days)).quantize(Decimal("0.01"))
        assert fee_items[0].amount == expected

    def test_couple_two_new_owners(self, setup):
        c3 = Customer(name="Dritt Besitzer", customer_number=3)
        db.session.add(c3)
        db.session.commit()
        _run(setup, new_ids=[setup["new"].id, c3.id])
        actives = PropertyOwnership.query.filter_by(
            property_id=setup["prop"].id, valid_to=None).all()
        assert {o.customer_id for o in actives} == {setup["new"].id, c3.id}

    def test_without_settlement_no_invoice(self, setup):
        oc, _ = _run(setup, create_settlement=False)
        assert oc.settlement_invoice_id is None
        assert Invoice.query.count() == 0
        # Snapshot wird trotzdem eingefroren.
        assert OwnerChangeMeterValue.query.filter_by(owner_change_id=oc.id).count() == 1

    def test_deductions_helper(self, setup):
        oc, _ = _run(setup)
        ded = svc.deductions_for_property(setup["prop"].id, setup["period"].id)
        assert ded is not None
        assert ded["total"] == Decimal("30")
        assert ded["by_meter"][setup["meter"].id] == Decimal("30")

    def test_cancelled_settlement_no_deduction(self, setup):
        oc, _ = _run(setup)
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        inv.status = Invoice.STATUS_CANCELLED
        db.session.commit()
        assert svc.deductions_for_property(setup["prop"].id, setup["period"].id) is None


class TestGuards:
    def test_stichtag_outside_period_rejected(self, setup):
        with pytest.raises(svc.OwnerChangeError):
            svc.execute_owner_change(
                prop=setup["prop"], period=setup["period"],
                stichtag=date(TODAY.year + 1, 2, 1),
                new_customer_ids=[setup["new"].id],
                meter_inputs={setup["meter"].id: {"value": Decimal("130")}},
                create_settlement=False, settlement_recipient_id=None,
                tariff=None, due_days=30, fee_mode=svc.FEE_MODE_NEW_OWNER_FULL,
                created_by_id=setup["user"].id)

    def test_existing_standard_invoice_blocks_settlement(self, setup):
        db.session.add(Invoice(
            invoice_number="X-1", customer_id=setup["old"].id,
            property_id=setup["prop"].id, billing_period_id=setup["period"].id,
            invoice_kind=Invoice.KIND_STANDARD, status=Invoice.STATUS_SENT,
            date=TODAY))
        db.session.commit()
        with pytest.raises(svc.OwnerChangeError):
            _run(setup)

    def test_same_owners_rejected(self, setup):
        with pytest.raises(svc.OwnerChangeError):
            _run(setup, new_ids=[setup["old"].id])


class TestChainedChange:
    def test_second_change_bills_from_prior_snapshot(self, setup):
        # Erster Wechsel: 100 -> 130 (30 m³ an Alt).
        _run(setup)
        # Zweiter Wechsel spaeter in derselben Periode: 130 -> 160.
        c3 = Customer(name="Dritt", customer_number=9)
        db.session.add(c3)
        db.session.commit()
        oc2, _ = svc.execute_owner_change(
            prop=setup["prop"], period=setup["period"],
            stichtag=date(TODAY.year, 9, 1),
            new_customer_ids=[c3.id],
            meter_inputs={setup["meter"].id: {"value": Decimal("160")}},
            create_settlement=True, settlement_recipient_id=setup["new"].id,
            tariff=setup["tariff"], due_days=30,
            fee_mode=svc.FEE_MODE_NEW_OWNER_FULL, created_by_id=setup["user"].id)
        mv = OwnerChangeMeterValue.query.filter_by(owner_change_id=oc2.id).one()
        # Basis ist der Snapshot des 1. Wechsels (130), nicht initial_value.
        assert mv.consumption_billed == Decimal("30")  # 160 - 130
        # Gesamt-Abzug telescoping: 30 + 30 = 60.
        ded = svc.deductions_for_property(setup["prop"].id, setup["period"].id)
        assert ded["total"] == Decimal("60")


class TestRealReadingProtection:
    def test_existing_real_reading_not_overwritten(self, setup):
        # Echte Ablesung am/after Stichtag existiert schon.
        save_reading(setup["meter"], setup["period"], Decimal("140"),
                     reading_date=_stichtag() + timedelta(days=10),
                     is_estimated=False)
        db.session.commit()
        oc, warnings = _run(setup, value=Decimal("130"), create_settlement=False)
        # Bestehende Ablesung bleibt (140), nicht ueberschrieben mit 130.
        reading = MeterReading.query.filter_by(
            meter_id=setup["meter"].id, billing_period_id=setup["period"].id).one()
        assert reading.value == Decimal("140")
        assert any("nicht" in w.lower() or "NICHT" in w for w in warnings)


class TestWgUpdates:
    def test_old_owner_resigned_and_new_member(self, setup):
        AppSetting.set("org.type", "cooperative")
        db.session.commit()
        svc.execute_owner_change(
            prop=setup["prop"], period=setup["period"], stichtag=_stichtag(),
            new_customer_ids=[setup["new"].id],
            meter_inputs={setup["meter"].id: {"value": Decimal("130")}},
            create_settlement=False, settlement_recipient_id=None,
            tariff=None, due_days=30, fee_mode=svc.FEE_MODE_NEW_OWNER_FULL,
            resign_customer_ids=[setup["old"].id],
            new_member_updates={setup["new"].id: {"status": "member",
                                                  "member_since": _stichtag()}},
            created_by_id=setup["user"].id)
        old = db.session.get(Customer, setup["old"].id)
        assert old.wg_status == "resigned"
        assert old.wg_member_until == _stichtag() - timedelta(days=1)
        new = db.session.get(Customer, setup["new"].id)
        assert new.member_since == _stichtag()


class TestEstimationRegression:
    def test_year_end_over_estimated_stichtag_with_sent_settlement_no_correction(self, setup):
        # Wechsel mit geschaetztem Stichtags-Stand + versendeter Schlussrechnung.
        oc, _ = svc.execute_owner_change(
            prop=setup["prop"], period=setup["period"], stichtag=_stichtag(),
            new_customer_ids=[setup["new"].id],
            meter_inputs={setup["meter"].id: {"value": Decimal("130"),
                                              "is_estimated": True}},
            create_settlement=True, settlement_recipient_id=setup["old"].id,
            tariff=setup["tariff"], due_days=30,
            fee_mode=svc.FEE_MODE_NEW_OWNER_FULL, created_by_id=setup["user"].id)
        inv = db.session.get(Invoice, oc.settlement_invoice_id)
        inv.status = Invoice.STATUS_SENT
        db.session.commit()
        # Echter Jahresend-Stand ueberschreibt die geschaetzte Stichtags-Ablesung.
        save_reading(setup["meter"], setup["period"], Decimal("200"),
                     reading_date=setup["period"].end_date, is_estimated=False)
        db.session.commit()
        # Es darf KEINE ReadingCorrection entstehen (die Schlussrechnung ist
        # kein 'standard'-Beleg -> _issued_invoice_for ignoriert sie).
        assert ReadingCorrection.query.count() == 0
