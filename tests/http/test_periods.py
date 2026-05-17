"""HTTP-Tests fuer das Abrechnungsperioden-Blueprint (/perioden)."""
from datetime import date

import pytest

from app.extensions import db
from app.models import BillingPeriod, User


@pytest.fixture
def admin(app):
    u = User(username="admin", email="a@a.test", role="admin")
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client):
    return client.post(
        "/auth/login", data={"username": "admin", "password": "secret"})


class TestPeriodsCrud:
    def test_index_renders(self, client, admin):
        _login(client)
        r = client.get("/perioden/")
        assert r.status_code == 200
        assert "Abrechnungsperioden" in r.get_data(as_text=True)

    def test_create_first_period_is_active(self, client, admin):
        _login(client)
        r = client.post("/perioden/neu", data={
            "name": "2025/26",
            "start_date": "2025-06-01",
            "end_date": "2026-05-31",
        }, follow_redirects=False)
        assert r.status_code == 302
        p = BillingPeriod.query.filter_by(name="2025/26").one()
        # Erste Periode ueberhaupt → automatisch aktiv.
        assert p.active is True

    def test_activate_switches_active_period(self, client, admin):
        _login(client)
        p1 = BillingPeriod(name="2024", start_date=date(2024, 1, 1),
                           end_date=date(2024, 12, 31), active=True)
        p2 = BillingPeriod(name="2025", start_date=date(2025, 1, 1),
                           end_date=date(2025, 12, 31), active=False)
        db.session.add_all([p1, p2])
        db.session.commit()

        r = client.post(f"/perioden/{p2.id}/aktivieren", follow_redirects=False)
        assert r.status_code == 302
        assert BillingPeriod.current().id == p2.id
        assert BillingPeriod.query.filter_by(active=True).count() == 1

    def test_end_before_start_rejected(self, client, admin):
        _login(client)
        r = client.post("/perioden/neu", data={
            "name": "Falsch",
            "start_date": "2026-05-31",
            "end_date": "2025-06-01",
        }, follow_redirects=True)
        assert r.status_code == 200
        assert BillingPeriod.query.filter_by(name="Falsch").count() == 0
