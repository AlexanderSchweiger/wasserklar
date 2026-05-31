"""HTTP-Tests fuer den interaktiven Zaehlertausch (``meters.meter_replace``).

Fokus: die Warnung, wenn die Abschlussablesung einen in der gewaehlten Periode
bereits vorhandenen Stand des alten Zaehlers ueberschreibt (analog zum
Tausch-Import -- kein stiller Datenverlust).
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
    u = User(username="admin", email="a@a.test", role_id=_ensure_role("Admin").id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post(
        "/auth/login", data={"username": "admin", "password": "secret"})


@pytest.fixture
def meter_with_period(app):
    p25 = BillingPeriod(name="2025", start_date=date(2025, 1, 1),
                        end_date=date(2025, 12, 31), active=True)
    db.session.add(p25)
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    m = WaterMeter(property_id=prop.id, meter_number="Z1",
                   initial_value=Decimal("0"), active=True)
    db.session.add(m)
    db.session.commit()
    return {"meter": m, "p25": p25}


class TestMeterReplaceOverwriteWarning:
    def test_warns_when_overwriting_existing_reading(self, client, admin, meter_with_period):
        m, p25 = meter_with_period["meter"], meter_with_period["p25"]
        save_reading(m, p25, Decimal("500"), reading_date=date(2025, 10, 1))
        db.session.commit()
        _login(client)

        r = client.post(f"/meters/{m.id}/replace", data={
            "billing_period_id": p25.id, "replacement_date": "2025-06-01",
            "final_value": "480", "new_meter_number": "Z2",
            "new_initial_value": "0", "from": "property",
        }, follow_redirects=True)
        html = r.get_data(as_text=True)
        assert "bereits ein Stand" in html
        assert "500" in html and "480" in html
        # Ausbau-Stand ersetzt den bestehenden
        assert MeterReading.query.filter_by(
            meter_id=m.id, billing_period_id=p25.id).one().value == Decimal("480")

    def test_no_warning_when_value_unchanged(self, client, admin, meter_with_period):
        m, p25 = meter_with_period["meter"], meter_with_period["p25"]
        save_reading(m, p25, Decimal("300"), reading_date=date(2025, 10, 1))
        db.session.commit()
        _login(client)

        r = client.post(f"/meters/{m.id}/replace", data={
            "billing_period_id": p25.id, "replacement_date": "2025-06-01",
            "final_value": "300", "new_meter_number": "Z2",
            "new_initial_value": "0", "from": "property",
        }, follow_redirects=True)
        assert "bereits ein Stand" not in r.get_data(as_text=True)

    def test_no_warning_when_no_existing_reading(self, client, admin, meter_with_period):
        m, p25 = meter_with_period["meter"], meter_with_period["p25"]
        _login(client)

        r = client.post(f"/meters/{m.id}/replace", data={
            "billing_period_id": p25.id, "replacement_date": "2025-06-01",
            "final_value": "120", "new_meter_number": "Z2",
            "new_initial_value": "0", "from": "property",
        }, follow_redirects=True)
        assert "bereits ein Stand" not in r.get_data(as_text=True)
        # Abschlussablesung wurde neu angelegt
        assert MeterReading.query.filter_by(
            meter_id=m.id, billing_period_id=p25.id).one().value == Decimal("120")
