"""HTTP-Tests fuer den Eigentuemerwechsel-Wizard (Routen, Rechte, Session)."""
from datetime import date
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    BillingPeriod, Customer, FiscalYear, OwnerChange, Property,
    PropertyOwnership, User, WaterMeter, WaterTariff,
)
from tests.conftest import _ensure_role

TODAY = date.today()
STICHTAG = date(TODAY.year, 7, 1)


def _mk_user(username, role_name, perms):
    role = _ensure_role(role_name, perms)
    u = User(username=username, email=f"{username}@t.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin"):
    client.get("/auth/logout")   # CookieJar teilt Login-State zwischen Tests
    return client.post("/auth/login", data={"username": username, "password": "secret"})


@pytest.fixture
def scenario(app):
    _mk_user("admin", "Admin", ())
    _mk_user("stamm", "StammOnly", ("stammdaten",))
    db.session.add(FiscalYear(
        year=TODAY.year, start_date=date(TODAY.year, 1, 1),
        end_date=date(TODAY.year, 12, 31)))
    period = BillingPeriod(
        name="P", start_date=date(TODAY.year, 1, 1),
        end_date=date(TODAY.year, 12, 31), active=True)
    db.session.add(period)
    tariff = WaterTariff(name="T", valid_from=TODAY.year,
                         base_fee=Decimal("30"), price_per_m3=Decimal("2"))
    db.session.add(tariff)
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
            "prop": prop, "meter": meter}


class TestAccess:
    def test_start_requires_login(self, client, scenario):
        client.get("/auth/logout")
        r = client.get(f"/owner-change/{scenario['prop'].id}/start")
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_start_requires_stammdaten(self, client, scenario):
        _mk_user("noperm", "NoPerm", ())
        _login(client, "noperm")
        r = client.get(f"/owner-change/{scenario['prop'].id}/start")
        assert r.status_code == 302   # Redirect zum Dashboard (kein Zugriff)
        assert "/owner-change/" not in r.headers["Location"]

    def test_settlement_step_forbidden_without_rechnungen(self, client, scenario):
        _login(client, "stamm")
        r = client.get(f"/owner-change/{scenario['prop'].id}/settlement")
        assert r.status_code == 302   # permission_required(rechnungen_op) -> Dashboard


class TestSession:
    def test_expired_session_redirects_to_start(self, client, scenario):
        _login(client)
        r = client.get(f"/owner-change/{scenario['prop'].id}/meters")
        assert r.status_code == 302
        assert r.headers["Location"].endswith(f"/owner-change/{scenario['prop'].id}/start")

    def test_start_get_renders(self, client, scenario):
        _login(client)
        r = client.get(f"/owner-change/{scenario['prop'].id}/start")
        assert r.status_code == 200
        assert "Eigentümerwechsel" in r.get_data(as_text=True)


class TestFullFlow:
    def _start(self, client, scenario, **extra):
        data = {
            "stichtag": STICHTAG.isoformat(),
            "period_id": str(scenario["period"].id),
            "new_customer_ids": str(scenario["new"].id),
        }
        data.update(extra)
        return client.post(f"/owner-change/{scenario['prop'].id}/start",
                           data=data, follow_redirects=False)

    def test_flow_without_settlement_executes(self, client, scenario):
        _login(client)
        pid = scenario["prop"].id
        r = self._start(client, scenario)
        assert r.status_code == 302
        assert r.headers["Location"].endswith(f"/{pid}/meters")

        # Zaehlerschritt
        assert client.get(f"/owner-change/{pid}/meters").status_code == 200
        r = client.post(f"/owner-change/{pid}/meters",
                        data={f"value_{scenario['meter'].id}": "130"})
        assert r.status_code == 302   # WG-Modus -> member

        # Mitgliedschafts-Schritt (WG-Modus default an)
        assert client.get(f"/owner-change/{pid}/member").status_code == 200
        r = client.post(f"/owner-change/{pid}/member", data={})
        assert r.status_code == 302   # kein Settlement -> confirm

        assert client.get(f"/owner-change/{pid}/confirm").status_code == 200
        r = client.post(f"/owner-change/{pid}/confirm", data={})
        assert r.status_code == 302

        oc = OwnerChange.query.one()
        assert oc.property_id == pid
        assert oc.settlement_invoice_id is None
        # Ownership umgeschrieben.
        new_ow = PropertyOwnership.query.filter_by(
            property_id=pid, valid_to=None).one()
        assert new_ow.customer_id == scenario["new"].id

    def test_flow_with_settlement(self, client, scenario):
        _login(client)
        pid = scenario["prop"].id
        self._start(client, scenario, create_settlement="1",
                    settlement_recipient_id=str(scenario["old"].id))
        client.post(f"/owner-change/{pid}/meters",
                    data={f"value_{scenario['meter'].id}": "130"})
        client.post(f"/owner-change/{pid}/member", data={})
        # Settlement-Schritt
        assert client.get(f"/owner-change/{pid}/settlement").status_code == 200
        r = client.post(f"/owner-change/{pid}/settlement", data={
            "tariff_id": str(scenario["tariff"].id), "due_days": "30",
            "fee_mode": "new_owner_full", "action": "continue"})
        assert r.status_code == 302
        assert client.get(f"/owner-change/{pid}/confirm").status_code == 200
        client.post(f"/owner-change/{pid}/confirm", data={})

        oc = OwnerChange.query.one()
        assert oc.settlement_invoice_id is not None

    def test_result_page_renders(self, client, scenario):
        _login(client)
        pid = scenario["prop"].id
        self._start(client, scenario)
        client.post(f"/owner-change/{pid}/meters",
                    data={f"value_{scenario['meter'].id}": "130"})
        client.post(f"/owner-change/{pid}/member", data={})
        client.post(f"/owner-change/{pid}/confirm", data={})
        oc = OwnerChange.query.one()
        r = client.get(f"/owner-change/{pid}/result/{oc.id}")
        assert r.status_code == 200
        assert "durchgeführt" in r.get_data(as_text=True)
