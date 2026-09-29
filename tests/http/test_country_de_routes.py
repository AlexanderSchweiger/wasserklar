"""HTTP-Tests der Länder-Anpassungen (Deutschland): manuelle Objektlage,
Adress-Abgleich ohne Photon, Wasserproben mit Grenzwert zum Probenahmedatum
und die länderabhängigen Seiten.

CSRF im Test aus; ``_login`` macht vorher ``/auth/logout`` (Cookie-Jar-Stolperer).
"""
from datetime import date

import pytest

from app import country
from app.extensions import db
from app.models import (
    AppSetting, NetworkFeature, NetworkPlan, Property, User, WaterSample,
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


def _login(client):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


def _set_country(code):
    AppSetting.set(country.SETTING_KEY, code)
    db.session.commit()


@pytest.fixture
def prop(app):
    p = Property(object_type="Haus", strasse="Hauptstraße", hausnummer="12",
                 plz="80331", ort="München", active=True)
    db.session.add(p)
    db.session.commit()
    return p


# ---------------------------------------------------------------------------
# Lage einer Liegenschaft manuell setzen (Fallback für beide Länder)
# ---------------------------------------------------------------------------

class TestSetLocation:
    def test_sets_coordinates(self, client, admin, prop):
        _login(client)
        r = client.post(f"/properties/{prop.id}/location",
                        data={"lat": "48,1372", "lng": "11.5754"}, follow_redirects=True)
        assert r.status_code == 200
        assert "Lage der Liegenschaft gespeichert" in r.get_data(as_text=True)
        db.session.refresh(prop)
        assert (prop.lat, prop.lng) == (48.1372, 11.5754)
        assert prop.geocoded_at is not None

    def test_coordinates_are_rounded(self, client, admin, prop):
        _login(client)
        client.post(f"/properties/{prop.id}/location",
                    data={"lat": "48.123456789", "lng": "11.987654321"})
        db.session.refresh(prop)
        assert (prop.lat, prop.lng) == (48.1234568, 11.9876543)

    def test_clear_removes_position(self, client, admin, prop):
        prop.lat, prop.lng = 48.0, 11.0
        db.session.commit()
        _login(client)
        r = client.post(f"/properties/{prop.id}/location", data={"clear": "1"},
                        follow_redirects=True)
        assert "Lage der Liegenschaft entfernt" in r.get_data(as_text=True)
        db.session.refresh(prop)
        assert prop.lat is None and prop.lng is None and prop.geocoded_at is None

    @pytest.mark.parametrize("data", [
        {"lat": "abc", "lng": "11"},
        {"lat": "", "lng": ""},
        {"lat": "91", "lng": "11"},
        {"lat": "48", "lng": "181"},
    ])
    def test_invalid_coordinates_are_rejected(self, client, admin, prop, data):
        _login(client)
        r = client.post(f"/properties/{prop.id}/location", data=data, follow_redirects=True)
        assert "Ungültige Koordinate" in r.get_data(as_text=True)
        db.session.refresh(prop)
        assert prop.lat is None

    def test_unknown_property_is_404(self, client, admin):
        _login(client)
        assert client.post("/properties/9999/location", data={"lat": "1", "lng": "1"}).status_code == 404

    def test_requires_login(self, client, prop):
        client.get("/auth/logout")
        r = client.post(f"/properties/{prop.id}/location", data={"lat": "1", "lng": "1"})
        assert r.status_code in (302, 401)
        db.session.refresh(prop)
        assert prop.lat is None


class TestAddressMatchDe:
    def test_de_without_photon_points_to_manual_placement(self, app, client, admin, prop,
                                                          monkeypatch):
        _set_country("DE")
        monkeypatch.setitem(app.config, "GEOCODER_PHOTON_URL", "")
        _login(client)
        r = client.post("/properties/geocode-bev", data={}, follow_redirects=True)
        text = r.get_data(as_text=True)
        assert "manuell auf der Karte setzen" in text
        db.session.refresh(prop)
        assert prop.lat is None

    def test_de_with_photon_geocodes(self, app, client, admin, prop, monkeypatch):
        from app.properties import photon_geocode
        _set_country("DE")
        monkeypatch.setitem(app.config, "GEOCODER_PHOTON_URL", "http://photon:2322")
        monkeypatch.setattr(photon_geocode, "_fetch", lambda q, u, t: {"features": [{
            "properties": {"street": "Hauptstraße", "housenumber": "12", "postcode": "80331"},
            "geometry": {"coordinates": [11.5754, 48.1372]}}]})
        _login(client)
        r = client.post("/properties/geocode-bev", data={}, follow_redirects=True)
        assert "OpenStreetMap (Photon)" in r.get_data(as_text=True)
        db.session.refresh(prop)
        assert (prop.lat, prop.lng) == (48.1372, 11.5754)


# ---------------------------------------------------------------------------
# Wasserproben in DE: Grenzwert gilt zum Probenahmedatum
# ---------------------------------------------------------------------------

@pytest.fixture
def probenahme(app):
    plan = NetworkPlan(name="Plan", status=NetworkPlan.STATUS_ACTIVE, maintenance_enabled=True)
    db.session.add(plan)
    db.session.flush()
    feat = NetworkFeature(plan_id=plan.id, geometry_kind=NetworkFeature.GEOMETRY_POINT,
                          feature_type="probenahme", name="PN1",
                          geometry='{"type": "Point", "coordinates": [11.5, 48.1]}')
    db.session.add(feat)
    db.session.commit()
    return feat


_MOD = {"X-From-Modal": "1"}


class TestWaterSamplesDe:
    def _post(self, client, feature, when, **values):
        data = {"sample_date": when, **{f"value__{k}": v for k, v in values.items()}}
        return client.post(f"/network/features/{feature.id}/samples", data=data, headers=_MOD)

    def test_blei_limit_steps_down_in_2028(self, client, admin, probenahme):
        _set_country("DE")
        _login(client)
        self._post(client, probenahme, "2027-06-01", blei="7")
        self._post(client, probenahme, "2028-06-01", blei="7")
        by_date = {s.sample_date: s.results[0] for s in
                   WaterSample.query.filter_by(feature_id=probenahme.id)}
        old, new = by_date[date(2027, 6, 1)], by_date[date(2028, 6, 1)]
        assert (old.status, old.limit_text) == ("ok", "10 µg/l")
        assert (new.status, new.limit_text) == ("alarm", "5 µg/l")

    def test_de_only_parameter_can_be_recorded(self, client, admin, probenahme):
        _set_country("DE")
        _login(client)
        self._post(client, probenahme, "2026-06-01", uran="12")
        result = WaterSample.query.filter_by(feature_id=probenahme.id).one().results[0]
        assert (result.parameter_key, result.status, result.limit_text) == (
            "uran", "alarm", "10 µg/l")

    def test_de_only_parameter_is_ignored_in_at(self, client, admin, probenahme):
        _login(client)
        r = self._post(client, probenahme, "2026-06-01", uran="12")
        assert WaterSample.query.filter_by(feature_id=probenahme.id).count() == 0
        assert "mindestens einen Laborwert" in r.get_data(as_text=True)

    def test_limits_page_names_the_regulation(self, client, admin):
        _login(client)
        page = client.get("/network/water-quality/limits").get_data(as_text=True)
        assert "österreichischen Trinkwasserverordnung" in page and "(TWV)" in page
        _set_country("DE")
        page = client.get("/network/water-quality/limits").get_data(as_text=True)
        assert "deutschen Trinkwasserverordnung" in page and "(TrinkwV)" in page
        assert "Uran" in page
        assert "Ab 12.01.2028 gilt 5 µg/l" in page      # Hinweis zum Übergangs-Grenzwert

    def test_limits_form_saves_override_for_de_parameter(self, client, admin):
        _set_country("DE")
        _login(client)
        client.post("/network/water-quality/limits", data={"limit__uran": "8"})
        assert AppSetting.get("water_quality.uran.limit") == "8"
        client.post("/network/water-quality/limits", data={"limit__uran": ""})
        assert AppSetting.get("water_quality.uran.limit") is None

    def test_overview_page_lists_de_parameters(self, client, admin, probenahme):
        _set_country("DE")
        _login(client)
        page = client.get("/network/water-quality").get_data(as_text=True)
        assert "TrinkwV-Beprobung" in page
        assert "Summe PFAS-20" in page
