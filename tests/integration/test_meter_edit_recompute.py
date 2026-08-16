"""Regression-Test: ``meter_edit`` muss die Verbrauchskette neu berechnen,
wenn ``initial_value`` geaendert wird.

``initial_value`` ist der Anker der Verbrauchskette (siehe
``app/meters/services.py:recompute_meter_chain``) -- der Verbrauch der
ersten Ablesung eines Zaehlers ist ``value - initial_value``. Vor dem Fix
schrieb ``meter_edit`` das neue ``initial_value`` in die DB, ohne die
bereits gespeicherten ``MeterReading.consumption``-Werte anzupassen.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.meters.services import save_reading
from app.models import BillingPeriod, MeterReading, Property, User, WaterMeter
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.test", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def prop(app):
    p = Property(object_number="P-1", object_type="Haus")
    db.session.add(p)
    db.session.commit()
    return p


def _login(client):
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


class TestMeterEditRecomputesConsumption:
    def test_initial_value_edit_updates_stored_consumption(self, client, admin, prop):
        meter = WaterMeter(property_id=prop.id, meter_number="Z-1",
                           initial_value=Decimal("100"), meter_type="main",
                           active=True)
        db.session.add(meter)
        db.session.flush()
        period = BillingPeriod(name="2025", start_date=date(2025, 1, 1),
                               end_date=date(2025, 12, 31), active=True)
        db.session.add(period)
        db.session.commit()

        save_reading(meter, period, Decimal("150"))
        db.session.commit()

        reading = MeterReading.query.filter_by(
            meter_id=meter.id, billing_period_id=period.id).one()
        assert reading.consumption == Decimal("50")

        _login(client)
        r = client.post(f"/meters/{meter.id}/edit", data={
            "property_id": str(prop.id),
            "meter_number": "Z-1",
            "meter_type": "main",
            "parent_meter_id": "",
            "initial_value": "120",
        }, follow_redirects=False)
        assert r.status_code == 302

        db.session.refresh(meter)
        assert meter.initial_value == Decimal("120")

        db.session.refresh(reading)
        assert reading.consumption == Decimal("30")

    def test_unrelated_edit_leaves_consumption_untouched(self, client, admin, prop):
        """Kein unnoetiges Recompute, wenn ``initial_value`` unveraendert bleibt."""
        meter = WaterMeter(property_id=prop.id, meter_number="Z-2",
                           initial_value=Decimal("100"), meter_type="main",
                           active=True)
        db.session.add(meter)
        db.session.flush()
        period = BillingPeriod(name="2025", start_date=date(2025, 1, 1),
                               end_date=date(2025, 12, 31), active=True)
        db.session.add(period)
        db.session.commit()

        save_reading(meter, period, Decimal("150"))
        db.session.commit()

        _login(client)
        r = client.post(f"/meters/{meter.id}/edit", data={
            "property_id": str(prop.id),
            "meter_number": "Z-2",
            "meter_type": "main",
            "parent_meter_id": "",
            "initial_value": "100",
            "location": "Keller",
        }, follow_redirects=False)
        assert r.status_code == 302

        reading = MeterReading.query.filter_by(
            meter_id=meter.id, billing_period_id=period.id).one()
        assert reading.consumption == Decimal("50")
