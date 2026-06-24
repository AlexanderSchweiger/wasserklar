"""Tests fuer den Hydrantenplan-Export (Feuerwehr): Bauart-Feld, Mehrtyp-Filter,
Druckansicht, oeffentliche Freigabe-Links (flag-gated) und das datenschutz-
sichere Public-GeoJSON.

CSRF ist im Test-Modus aus (TestingConfig). Cookie-Jar-Stolperer (Werkzeug 3):
``_login`` macht vorher ``/auth/logout``.
"""
import pytest

from app.extensions import db
from app.models import NetworkPlan, NetworkFeature, HydrantShareLink, User
from app.network import services as svc
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


@pytest.fixture
def share_enabled(app):
    """FEATURE_HYDRANT_PUBLIC_SHARE temporaer einschalten (Default aus im OSS)."""
    prev = app.config.get("FEATURE_HYDRANT_PUBLIC_SHARE")
    app.config["FEATURE_HYDRANT_PUBLIC_SHARE"] = True
    yield
    app.config["FEATURE_HYDRANT_PUBLIC_SHARE"] = prev


def _login(client, username="admin", password="secret"):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": username, "password": password})


def _make_point(client, ftype="hydrant", lng=16.37, lat=48.21, **props):
    body = {"geometry": {"type": "Point", "coordinates": [lng, lat]}, "feature_type": ftype}
    body.update(props)
    return client.post("/network/features", json=body)


class TestHydrantTypeField:
    def test_hydrant_type_saved_and_serialized(self, client, admin):
        _login(client)
        fid = _make_point(client, name="H1").get_json()["id"]
        # Bauart via Panel-Formular speichern.
        r = client.post(f"/network/features/{fid}", data={
            "feature_type": "hydrant", "name": "H1", "hydrant_type": "ueberflur",
        })
        assert r.status_code == 200
        f = db.session.get(NetworkFeature, fid)
        assert f.hydrant_type == "ueberflur"

        gj = svc.feature_to_geojson(f)
        assert gj["properties"]["hydrant_type"] == "ueberflur"
        assert gj["properties"]["hydrant_type_label"] == "Überflurhydrant"

    def test_invalid_hydrant_type_rejected(self, client, admin):
        _login(client)
        fid = _make_point(client).get_json()["id"]
        client.post(f"/network/features/{fid}", data={
            "feature_type": "hydrant", "hydrant_type": "voll-erfunden",
        })
        assert db.session.get(NetworkFeature, fid).hydrant_type is None


class TestMultiTypeFilter:
    def test_features_geojson_filters_multiple_types(self, client, admin):
        _login(client)
        _make_point(client, ftype="hydrant", lng=16.30, lat=48.20)
        _make_point(client, ftype="schieber", lng=16.31, lat=48.21)
        # Linie (Versorgungsleitung) anlegen.
        client.post("/network/features", json={
            "geometry": {"type": "LineString", "coordinates": [[16.3, 48.2], [16.31, 48.21]]},
            "feature_type": "versorgungsleitung",
        })
        r = client.get("/network/features.geojson?type=hydrant&type=versorgungsleitung")
        types = {f["properties"]["feature_type"] for f in r.get_json()["features"]}
        assert types == {"hydrant", "versorgungsleitung"}  # schieber gefiltert


class TestHydrantsPrint:
    def test_print_loads(self, client, admin):
        _login(client)
        _make_point(client, name="H1", hydrant_type="ueberflur")
        r = client.get("/network/hydrants/print")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Hydrantenplan" in body
        assert "Hydranten-Verzeichnis" in body

    def test_share_ui_hidden_without_flag(self, client, admin):
        _login(client)
        r = client.get("/network/hydrants/print")
        assert "Öffentliche Feuerwehr-Links" not in r.get_data(as_text=True)

    def test_share_ui_visible_with_flag(self, client, admin, share_enabled):
        _login(client)
        r = client.get("/network/hydrants/print")
        assert "Öffentliche Feuerwehr-Links" in r.get_data(as_text=True)


class TestShareLinks:
    def test_create_requires_flag(self, client, admin):
        _login(client)
        r = client.post("/network/hydrants/share-links", data={"label": "FF X"})
        assert r.status_code == 404  # Flag aus -> Route nicht verfuegbar

    def test_create_and_revoke(self, client, admin, active_plan, share_enabled):
        _login(client)
        r = client.post("/network/hydrants/share-links",
                        data={"label": "FF Musterdorf"}, follow_redirects=False)
        assert r.status_code == 302
        link = HydrantShareLink.query.filter_by(plan_id=active_plan.id).first()
        assert link is not None
        assert link.label == "FF Musterdorf"
        assert len(link.token) >= 32
        assert link.is_active and link.is_valid()

        client.post(f"/network/hydrants/share-links/{link.id}/revoke")
        db.session.refresh(link)
        assert link.is_active is False
        assert link.is_valid() is False


class TestPublicCollectionPrivacy:
    def test_public_geojson_omits_sensitive_fields(self, app, active_plan):
        f = NetworkFeature(
            plan_id=active_plan.id, geometry_kind="point", feature_type="hydrant",
            name="H1", geometry='{"type":"Point","coordinates":[16.37,48.21]}',
            lat=48.21, lng=16.37, dimension_dn=100, hydrant_type="unterflur",
            pressure_rating="PN 10", notes="INTERN: Schlüssel beim Obmann",
        )
        db.session.add(f)
        db.session.commit()

        coll = svc.public_network_collection([f])
        assert len(coll["features"]) == 1
        props = coll["features"][0]["properties"]
        # FW-relevante Felder vorhanden ...
        assert props["hydrant_type"] == "unterflur"
        assert props["dimension_dn"] == 100
        # ... aber KEINE sensiblen/internen Felder.
        for forbidden in ("notes", "owner_names", "property_label",
                          "property_address", "meter_id", "property_id"):
            assert forbidden not in props

    def test_public_collection_hard_type_filter(self, app, active_plan):
        # Ein Hausanschluss darf NIE im oeffentlichen Plan landen.
        ha = NetworkFeature(
            plan_id=active_plan.id, geometry_kind="point", feature_type="hausanschluss",
            name="HA1", geometry='{"type":"Point","coordinates":[16.37,48.21]}',
            lat=48.21, lng=16.37,
        )
        db.session.add(ha)
        db.session.commit()
        coll = svc.public_network_collection([ha])
        assert coll["features"] == []
