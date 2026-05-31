"""HTTP-Tests fuer das Loeschen einzelner Zaehlerstaende und die
Verbrauchs-Neuberechnung danach (``meters.reading_delete``).

Begleitet das Loeschen-Feature im Ablese-Modal: ein eigener POST-Endpoint
loescht den Stand und rechnet die Verbrauchskette des Zaehlers neu, sodass die
Folge-Ablesung den Luecken-Stand ueberbrueckt.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.meters.services import save_reading
from app.models import (
    BillingPeriod, MeterReading, Property, User, WaterMeter,
)
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    admin_role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=admin_role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post(
        "/auth/login", data={"username": "admin", "password": "secret"})


@pytest.fixture
def meter_with_readings(app):
    """Zaehler (initial_value 0) mit drei Ablesungen ueber drei Perioden:
    2023->100, 2024->175, 2025->230 (Verbrauch 100 / 75 / 55)."""
    p23 = BillingPeriod(name="2023", start_date=date(2023, 1, 1),
                        end_date=date(2023, 12, 31))
    p24 = BillingPeriod(name="2024", start_date=date(2024, 1, 1),
                        end_date=date(2024, 12, 31))
    p25 = BillingPeriod(name="2025", start_date=date(2025, 1, 1),
                        end_date=date(2025, 12, 31), active=True)
    db.session.add_all([p23, p24, p25])
    prop = Property(object_number="P-1", object_type="Haus")
    db.session.add(prop)
    db.session.flush()
    meter = WaterMeter(property_id=prop.id, meter_number="Z-1",
                       meter_type="main", initial_value=Decimal("0"))
    db.session.add(meter)
    db.session.flush()
    save_reading(meter, p23, Decimal("100"), reading_date=date(2023, 12, 31))
    save_reading(meter, p24, Decimal("175"), reading_date=date(2024, 12, 31))
    save_reading(meter, p25, Decimal("230"), reading_date=date(2025, 12, 31))
    db.session.commit()
    return {"meter": meter, "p23": p23, "p24": p24, "p25": p25}


class TestReadingDelete:
    def test_delete_removes_reading(self, client, admin, meter_with_readings):
        _login(client)
        m = meter_with_readings["meter"]
        r24 = MeterReading.query.filter_by(
            meter_id=m.id,
            billing_period_id=meter_with_readings["p24"].id).one()
        resp = client.post(f"/meters/reading/{r24.id}/delete")
        assert resp.status_code == 302
        assert db.session.get(MeterReading, r24.id) is None

    def test_delete_modal_returns_204_with_events(
            self, client, admin, meter_with_readings):
        _login(client)
        m = meter_with_readings["meter"]
        r24 = MeterReading.query.filter_by(
            meter_id=m.id,
            billing_period_id=meter_with_readings["p24"].id).one()
        resp = client.post(f"/meters/reading/{r24.id}/delete",
                          headers={"X-From-Modal": "1"})
        assert resp.status_code == 204
        trigger = resp.headers.get("HX-Trigger", "")
        assert "closeReadingModal" in trigger
        assert "readingSaved" in trigger

    def test_delete_middle_bridges_following_consumption(
            self, client, admin, meter_with_readings):
        # 2024 loeschen -> 2025-Verbrauch ueberbrueckt auf 2023: 230 - 100 = 130.
        _login(client)
        m = meter_with_readings["meter"]
        r24 = MeterReading.query.filter_by(
            meter_id=m.id,
            billing_period_id=meter_with_readings["p24"].id).one()
        client.post(f"/meters/reading/{r24.id}/delete")
        r25 = MeterReading.query.filter_by(
            meter_id=m.id,
            billing_period_id=meter_with_readings["p25"].id).one()
        assert r25.consumption == Decimal("130")

    def test_delete_first_falls_back_to_initial(
            self, client, admin, meter_with_readings):
        # Ersten Stand (2023) loeschen -> 2024 rechnet gegen initial_value 0.
        _login(client)
        m = meter_with_readings["meter"]
        r23 = MeterReading.query.filter_by(
            meter_id=m.id,
            billing_period_id=meter_with_readings["p23"].id).one()
        client.post(f"/meters/reading/{r23.id}/delete")
        r24 = MeterReading.query.filter_by(
            meter_id=m.id,
            billing_period_id=meter_with_readings["p24"].id).one()
        assert r24.consumption == Decimal("175")   # 175 - 0

    def test_delete_button_only_when_existing(
            self, client, admin, meter_with_readings):
        """Der Modal-Body zeigt den Loeschen-Button nur fuer eine vorhandene
        Ablesung — beim erstmaligen Ablesen (Periode ohne Stand) nicht."""
        _login(client)
        m = meter_with_readings["meter"]
        # Periode mit existierender Ablesung -> Button da.
        with_existing = client.get(
            f"/meters/{m.id}/read?period_id={meter_with_readings['p24'].id}",
            headers={"HX-Request": "true"})
        assert "reading/" in with_existing.get_data(as_text=True)
        assert "Zählerstand löschen" in with_existing.get_data(as_text=True)
        # Neue Periode ohne Ablesung -> kein Button.
        empty = BillingPeriod(name="2099", start_date=date(2099, 1, 1),
                            end_date=date(2099, 12, 31))
        db.session.add(empty)
        db.session.commit()
        without = client.get(
            f"/meters/{m.id}/read?period_id={empty.id}",
            headers={"HX-Request": "true"})
        assert "Zählerstand löschen" not in without.get_data(as_text=True)

    def test_login_required(self, client, meter_with_readings):
        client.get("/auth/logout")
        m = meter_with_readings["meter"]
        r24 = MeterReading.query.filter_by(
            meter_id=m.id,
            billing_period_id=meter_with_readings["p24"].id).one()
        resp = client.post(f"/meters/reading/{r24.id}/delete")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]
        # Nichts geloescht.
        assert db.session.get(MeterReading, r24.id) is not None
