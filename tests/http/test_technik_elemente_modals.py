"""HTTP-Tests fuer die zwei Elementliste-Modals (Element bearbeiten + Wartung &
Prüfung) sowie das weiterhin von der Karte genutzte Feature-Panel.

- Element-bearbeiten-Modal: GET /features/<id>/edit (Felder-Fragment), POST speichert
  und schliesst (204 + HX-Trigger closeFeatureEditModal).
- Wartungs-Modal: GET/POST /features/<id>/maintenance liefern bei X-From-Modal das
  Modal-Body-Fragment (Modal bleibt offen), ohne den Header das volle Karten-Panel.

CSRF im Test aus. Cookie-Jar-Stolperer: ``_login`` macht vorher ``/auth/logout``.
"""
from datetime import date

import pytest

from app.extensions import db
from app.models import (
    NetworkPlan, NetworkFeature, MaintenanceLog, Property, PropertyOwnership, Customer, User,
)
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture(autouse=True)
def active_plan(app):
    p = NetworkPlan(name="Testplan", status=NetworkPlan.STATUS_ACTIVE, maintenance_enabled=True)
    db.session.add(p)
    db.session.commit()
    return p


def _login(client, username="admin", password="secret"):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": username, "password": password})


def _make_point(client, ftype="hydrant", lng=16.37, lat=48.21, **props):
    body = {"geometry": {"type": "Point", "coordinates": [lng, lat]}, "feature_type": ftype}
    body.update(props)
    return client.post("/technik/features", json=body)


def _customer(name):
    c = Customer(name=name)
    db.session.add(c)
    db.session.commit()
    return c


def _property(object_number, otype="Haus"):
    p = Property(object_number=object_number, object_type=otype)
    db.session.add(p)
    db.session.commit()
    return p


def _own(prop, cust):
    o = PropertyOwnership(property_id=prop.id, customer_id=cust.id,
                          valid_from=date(2020, 1, 1), valid_to=None)
    db.session.add(o)
    db.session.commit()
    return o


_MOD = {"X-From-Modal": "1"}


class TestFeaturePanel:
    """Das Karten-Panel (technik.feature_panel) ist unveraendert in Verwendung."""

    def test_panel_get_renders_form_and_maintenance(self, client, admin):
        _login(client)
        fid = _make_point(client, name="M1").get_json()["id"]
        body = client.get(f"/technik/features/{fid}").get_data(as_text=True)
        assert 'name="feature_type"' in body
        assert "Lagegenauigkeit" in body
        assert "Wartung" in body          # Panel enthält Wartungssektion
        assert "Fotos" in body            # ... und Fotos

    def test_attribute_save_emits_featuresaved_trigger(self, client, admin):
        _login(client)
        fid = _make_point(client, name="M2").get_json()["id"]
        r = client.post(f"/technik/features/{fid}", data={
            "feature_type": "schieber", "name": "M2-neu", "accuracy": "exakt",
        }, headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert "technik:featureSaved" in r.headers.get("HX-Trigger", "")
        f = db.session.get(NetworkFeature, fid)
        assert f.name == "M2-neu" and f.feature_type == "schieber"

    def test_delete_emits_featuredeleted_trigger(self, client, admin):
        _login(client)
        fid = _make_point(client, name="M3").get_json()["id"]
        r = client.post(f"/technik/features/{fid}/delete")
        assert r.status_code == 200
        assert "technik:featureDeleted" in r.headers.get("HX-Trigger", "")
        assert db.session.get(NetworkFeature, fid) is None

    def test_panel_has_no_meter_select(self, client, admin):
        _login(client)
        fid = _make_point(client, name="M4").get_json()["id"]
        body = client.get(f"/technik/features/{fid}").get_data(as_text=True)
        assert 'name="property_id"' in body
        assert 'name="meter_id"' not in body
        assert "Wasserzähler" not in body

    def test_panel_property_option_shows_owner(self, client, admin):
        _login(client)
        cust = _customer("Familie Huber")
        prop = _property("OBJ-7")
        _own(prop, cust)
        fid = _make_point(client, name="M5").get_json()["id"]
        body = client.get(f"/technik/features/{fid}").get_data(as_text=True)
        assert "OBJ-7" in body
        assert "Familie Huber" in body


class TestEditModal:
    def test_get_returns_fields_only(self, client, admin):
        _login(client)
        fid = _make_point(client, name="E1").get_json()["id"]
        r = client.get(f"/technik/features/{fid}/edit", headers=_MOD)
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'name="feature_type"' in body
        assert 'name="property_id"' in body
        # Nur Stammdaten — keine Wartung/Fotos im Bearbeiten-Modal:
        assert "Wartung" not in body
        assert "Fotos" not in body

    def test_post_saves_and_closes(self, client, admin):
        _login(client)
        fid = _make_point(client, name="E2").get_json()["id"]
        r = client.post(f"/technik/features/{fid}/edit", data={
            "feature_type": "schieber", "name": "E2-neu", "accuracy": "gut",
            "material": "Stahl", "dimension_dn": "80",
        }, headers=_MOD)
        assert r.status_code == 204
        assert "closeFeatureEditModal" in r.headers.get("HX-Trigger", "")
        f = db.session.get(NetworkFeature, fid)
        assert f.name == "E2-neu"
        assert f.feature_type == "schieber"
        assert f.dimension_dn == 80


class TestMaintenanceModal:
    def test_get_returns_body(self, client, admin):
        _login(client)
        fid = _make_point(client, name="W1").get_json()["id"]
        r = client.get(f"/technik/features/{fid}/maintenance", headers=_MOD)
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'name="kind"' in body           # Eingabefelder vorhanden
        assert 'name="feature_type"' not in body  # KEIN Stammdaten-Formular

    def test_add_returns_body_and_stays_open(self, client, admin):
        _login(client)
        fid = _make_point(client, name="W2").get_json()["id"]
        r = client.post(f"/technik/features/{fid}/maintenance", data={
            "date": "2026-01-01", "kind": "wartung", "interval_months": "12",
        }, headers=_MOD)
        assert r.status_code == 200                # Body-Fragment, kein 204/Redirect
        body = r.get_data(as_text=True)
        assert 'name="kind"' in body               # Felder fuer den naechsten Eintrag bleiben
        log = MaintenanceLog.query.filter_by(feature_id=fid).one()
        assert log.kind == "wartung"
        assert log.next_due == date(2027, 1, 1)

    def test_delete_returns_body(self, client, admin):
        _login(client)
        fid = _make_point(client, name="W3").get_json()["id"]
        client.post(f"/technik/features/{fid}/maintenance",
                    data={"date": "2026-01-01", "kind": "inspektion"}, headers=_MOD)
        log = MaintenanceLog.query.filter_by(feature_id=fid).one()
        r = client.post(f"/technik/maintenance/{log.id}/delete", headers=_MOD)
        assert r.status_code == 200
        assert 'name="kind"' in r.get_data(as_text=True)
        assert MaintenanceLog.query.filter_by(feature_id=fid).count() == 0

    def test_map_context_still_returns_panel(self, client, admin):
        """Ohne X-From-Modal (Karten-Panel) kommt weiterhin das volle Panel."""
        _login(client)
        fid = _make_point(client, name="W4").get_json()["id"]
        r = client.post(f"/technik/features/{fid}/maintenance",
                        data={"date": "2026-01-01", "kind": "spuelung"})
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'name="feature_type"' in body       # volles Panel (mit Stammdaten-Form)
        assert MaintenanceLog.query.filter_by(feature_id=fid).count() == 1
