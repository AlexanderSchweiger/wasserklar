"""HTTP-/Flow-Tests: Rechnungslauf nach einem Eigentuemerwechsel.

Prueft, dass die Schlussrechnung den Jahreslauf NICHT blockiert und der
Nachbesitzer nur den Restverbrauch (bzw. anteilige Gebuehr) verrechnet bekommt.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.owner_change import services as svc
from app.meters.services import save_reading
from app.models import (
    Account, BillingPeriod, Customer, FiscalYear, Invoice, Property,
    PropertyOwnership, User, WaterMeter, WaterTariff,
)
from tests.conftest import _ensure_role

TODAY = date.today()


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


@pytest.fixture
def scenario(app, admin):
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
    db.session.add(Account(name="Wasser"))
    old = Customer(name="Alt", customer_number=1)
    new = Customer(name="Neu", customer_number=2)
    db.session.add_all([old, new])
    db.session.flush()
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    db.session.add(PropertyOwnership(
        property_id=prop.id, customer_id=old.id,
        valid_from=date(TODAY.year - 2, 1, 1), valid_to=None))
    meter = WaterMeter(property_id=prop.id, meter_number="Z-1",
                       meter_type="main", initial_value=Decimal("100"))
    db.session.add(meter)
    db.session.commit()
    return {"period": period, "tariff": tariff, "old": old, "new": new,
            "prop": prop, "meter": meter, "admin": admin}


def _change(scenario, *, fee_mode=svc.FEE_MODE_NEW_OWNER_FULL, stichtag_value=Decimal("130")):
    return svc.execute_owner_change(
        prop=scenario["prop"], period=scenario["period"],
        stichtag=date(TODAY.year, 7, 1),
        new_customer_ids=[scenario["new"].id],
        meter_inputs={scenario["meter"].id: {"value": stichtag_value}},
        create_settlement=True, settlement_recipient_id=scenario["old"].id,
        tariff=scenario["tariff"], due_days=30, fee_mode=fee_mode,
        created_by_id=scenario["admin"].id)


def _year_end(scenario, value):
    """Jahresend-Ablesung ueberschreibt die Stichtags-Ablesung (Upsert)."""
    save_reading(scenario["meter"], scenario["period"], Decimal(value),
                 reading_date=scenario["period"].end_date)
    db.session.commit()


def _generate(client, scenario):
    return client.post("/invoices/generate", data={
        "billing_period_id": str(scenario["period"].id),
        "tariff_id": str(scenario["tariff"].id),
        "due_days": "30",
    }, follow_redirects=False)


class TestBillingRunAfterChange:
    def test_annual_invoice_created_not_skipped(self, client, scenario):
        _change(scenario)
        _year_end(scenario, "200")
        _login(client)
        _generate(client, scenario)
        # Standard-Rechnung des Neubesitzers entsteht ZUSAETZLICH zur Schlussrechnung.
        std = Invoice.query.filter_by(invoice_kind=Invoice.KIND_STANDARD).all()
        assert len(std) == 1
        assert std[0].customer_id == scenario["new"].id

    def test_remainder_consumption_billed(self, client, scenario):
        _change(scenario)               # 30 m³ an Alt (130-100)
        _year_end(scenario, "200")      # Jahresverbrauch 100 (200-100)
        _login(client)
        _generate(client, scenario)
        std = Invoice.query.filter_by(invoice_kind=Invoice.KIND_STANDARD).one()
        m3 = [i for i in std.items if i.unit == "m³"][0]
        assert m3.quantity == Decimal("70")   # 100 - 30

    def test_new_owner_full_keeps_full_base_fee(self, client, scenario):
        _change(scenario, fee_mode=svc.FEE_MODE_NEW_OWNER_FULL)
        _year_end(scenario, "200")
        _login(client)
        _generate(client, scenario)
        std = Invoice.query.filter_by(invoice_kind=Invoice.KIND_STANDARD).one()
        fee = [i for i in std.items if i.unit == "Pauschal"][0]
        assert fee.amount == Decimal("36.50")   # volle Grundgebuehr

    def test_pro_rata_reduces_base_fee(self, client, scenario):
        _change(scenario, fee_mode=svc.FEE_MODE_PRO_RATA)
        _year_end(scenario, "200")
        _login(client)
        _generate(client, scenario)
        std = Invoice.query.filter_by(invoice_kind=Invoice.KIND_STANDARD).one()
        fee = [i for i in std.items if i.unit == "Pauschal"][0]
        period_days = (scenario["period"].end_date - scenario["period"].start_date).days + 1
        old_days = (date(TODAY.year, 7, 1) - scenario["period"].start_date).days
        remaining = period_days - old_days
        expected = (Decimal("36.50") * Decimal(remaining) / Decimal(period_days)).quantize(Decimal("0.01"))
        assert fee.amount == expected

    def test_cancelled_settlement_no_deduction(self, client, scenario):
        oc = _change(scenario)
        db.session.get(Invoice, oc[0].settlement_invoice_id).status = Invoice.STATUS_CANCELLED
        _year_end(scenario, "200")
        db.session.commit()
        _login(client)
        _generate(client, scenario)
        std = Invoice.query.filter_by(invoice_kind=Invoice.KIND_STANDARD).one()
        m3 = [i for i in std.items if i.unit == "m³"][0]
        assert m3.quantity == Decimal("100")   # kein Abzug -> voller Verbrauch

    def test_negative_remainder_clamped(self, client, scenario):
        _change(scenario, stichtag_value=Decimal("130"))   # 30 m³ an Alt
        _year_end(scenario, "120")     # Jahresverbrauch nur 20 (120-100) < 30
        _login(client)
        r = _generate(client, scenario)
        assert r.status_code == 302
        std = Invoice.query.filter_by(invoice_kind=Invoice.KIND_STANDARD).one()
        m3 = [i for i in std.items if i.unit == "m³"][0]
        assert m3.quantity == Decimal("0")   # auf 0 gekappt
