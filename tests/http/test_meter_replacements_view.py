"""HTTP-Tests fuer das Zaehlertausch-Event und die Historie-Ansicht.

Deckt ab:
- ``meter_replace`` legt ein ``MeterReplacement``-Event an.
- ``meter_delete`` blockt einen Zaehler, der Teil eines dokumentierten Tauschs
  ist (freundlicher Flash statt 500 ueber den RESTRICT-FK).
- ``GET /meters/replacements`` ist login-required und listet die Tausche.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    BillingPeriod, MeterReplacement, Property, User, WaterMeter,
)
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
    return {"meter": m, "p25": p25, "prop": prop}


def _do_replace(client, meter_id, period_id, *, new_number="Z2"):
    return client.post(f"/meters/{meter_id}/replace", data={
        "billing_period_id": period_id, "replacement_date": "2025-06-01",
        "final_value": "480", "new_meter_number": new_number,
        "new_initial_value": "0", "from": "property",
    }, follow_redirects=True)


class TestMeterReplaceCreatesEvent:
    def test_replace_writes_event(self, client, admin, meter_with_period):
        m, p25, prop = (meter_with_period[k] for k in ("meter", "p25", "prop"))
        _login(client)
        _do_replace(client, m.id, p25.id)

        new = WaterMeter.query.filter_by(meter_number="Z2").one()
        ev = MeterReplacement.query.one()
        assert ev.old_meter_id == m.id
        assert ev.new_meter_id == new.id
        assert ev.property_id == prop.id
        assert ev.billing_period_id == p25.id
        assert ev.replacement_date == date(2025, 6, 1)
        assert ev.final_value == Decimal("480")
        assert ev.new_initial_value == Decimal("0")
        assert ev.created_by_id == admin.id


class TestMeterDeleteGuard:
    def test_event_referenced_meter_cannot_be_deleted(self, client, admin, meter_with_period):
        m, p25 = meter_with_period["meter"], meter_with_period["p25"]
        _login(client)
        _do_replace(client, m.id, p25.id)
        new = WaterMeter.query.filter_by(meter_number="Z2").one()

        # Der neue Zaehler hat keine Ablesung -> der bestehende Ablese-Guard
        # greift NICHT; der neue Tausch-Guard muss ihn dennoch blocken.
        r = client.post(f"/meters/{new.id}/delete", follow_redirects=True)
        assert "dokumentierten Zählertauschs" in r.get_data(as_text=True)
        assert db.session.get(WaterMeter, new.id) is not None


class TestReplacementsView:
    def test_login_required(self, client):
        client.get("/auth/logout")
        r = client.get("/meters/replacements")
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_lists_replacements(self, client, admin, meter_with_period):
        m, p25 = meter_with_period["meter"], meter_with_period["p25"]
        _login(client)
        _do_replace(client, m.id, p25.id)

        r = client.get("/meters/replacements")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Z1" in html and "Z2" in html
        assert "Zählertausch-Historie" in html

    def test_search_filters(self, client, admin, meter_with_period):
        m, p25 = meter_with_period["meter"], meter_with_period["p25"]
        _login(client)
        _do_replace(client, m.id, p25.id)

        # Treffer auf neue Zaehlernummer
        r = client.get("/meters/replacements?q=Z2")
        assert "Z2" in r.get_data(as_text=True)
        # Kein Treffer
        r = client.get("/meters/replacements?q=NOPE-XYZ")
        assert "Keine Zählertausche gefunden." in r.get_data(as_text=True)
