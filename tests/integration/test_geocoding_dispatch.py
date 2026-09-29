"""Geocoding je Land: AT = BEV-Adressregister, DE = OpenStreetMap über Photon
(app/properties/geocoding.py, app/properties/photon_geocode.py).

Photon wird nie wirklich angefragt — ``photon_geocode._fetch`` ist gemockt."""
import pytest

from app import country
from app.extensions import db
from app.models import AppSetting, Property
from app.properties import geocoding, photon_geocode


def _set_country(code):
    AppSetting.set(country.SETTING_KEY, code)
    db.session.commit()


def _feature(street, hnr, plz, lon, lat):
    props = {"street": street, "housenumber": hnr}
    if plz is not None:
        props["postcode"] = plz
    return {"properties": props, "geometry": {"type": "Point", "coordinates": [lon, lat]}}


@pytest.fixture
def photon(app, monkeypatch):
    """Photon „konfiguriert" + programmierbare Antwort; merkt sich die Queries."""
    monkeypatch.setitem(app.config, "GEOCODER_PHOTON_URL", "http://photon:2322/")
    state = {"features": [], "queries": [], "error": None}

    def fake_fetch(query, url, timeout):
        state["queries"].append((query, url))
        if state["error"]:
            raise photon_geocode.PhotonError(state["error"])
        return {"features": state["features"]}

    monkeypatch.setattr(photon_geocode, "_fetch", fake_fetch)
    return state


class TestProviderSelection:
    def test_austria_uses_bev(self, app):
        assert geocoding.provider() == geocoding.PROVIDER_BEV

    def test_germany_uses_photon(self, app):
        _set_country("DE")
        assert geocoding.provider() == geocoding.PROVIDER_PHOTON
        assert "OpenStreetMap" in geocoding.LABELS[geocoding.provider()]

    def test_status_bev_without_index(self, app, monkeypatch, tmp_path):
        monkeypatch.setitem(app.config, "BEV_INDEX_PATH", str(tmp_path / "missing.sqlite"))
        st = geocoding.status()
        assert st["provider"] == "bev"
        assert st["available"] is False and st["index"] is None

    def test_status_photon_needs_url(self, app, monkeypatch):
        _set_country("DE")
        monkeypatch.setitem(app.config, "GEOCODER_PHOTON_URL", "")
        assert geocoding.status()["available"] is False
        monkeypatch.setitem(app.config, "GEOCODER_PHOTON_URL", " http://photon:2322/ ")
        st = geocoding.status()
        assert st["available"] is True and st["provider"] == "photon"
        assert photon_geocode.base_url() == "http://photon:2322"


class TestPhotonLookup:
    def test_exact_hit_returns_lat_lng(self, app, photon):
        photon["features"] = [_feature("Hauptstraße", "12", "80331", 11.5754, 48.1372)]
        assert photon_geocode.lookup("Hauptstraße", "12", "80331", "München") == (48.1372, 11.5754)
        # Query = „Straße Nr PLZ Ort" gegen den konfigurierten Server
        assert photon["queries"] == [("Hauptstraße 12 80331 München", "http://photon:2322")]

    def test_tolerant_street_and_number_spelling(self, app, photon):
        photon["features"] = [_feature("Hauptstrasse", "12a", "80331", 11.0, 48.0)]
        assert photon_geocode.lookup("Haupt Str.", "12 A", "80331") == (48.0, 11.0)

    def test_door_suffix_falls_back_to_leading_number(self, app, photon):
        photon["features"] = [_feature("Hauptstraße", "12a", None, 11.0, 48.0)]
        assert photon_geocode.lookup("Hauptstraße", "12a/3") == (48.0, 11.0)

    def test_other_street_is_not_a_hit(self, app, photon):
        """Photon liefert bei Unschärfe gern die Nachbarstraße — kein Treffer."""
        photon["features"] = [_feature("Bahnhofstraße", "12", "80331", 11.0, 48.0)]
        assert photon_geocode.lookup("Hauptstraße", "12", "80331") is None

    def test_other_house_number_is_not_a_hit(self, app, photon):
        photon["features"] = [_feature("Hauptstraße", "14", "80331", 11.0, 48.0)]
        assert photon_geocode.lookup("Hauptstraße", "12", "80331") is None

    def test_wrong_postcode_is_not_a_hit_but_missing_postcode_is(self, app, photon):
        photon["features"] = [_feature("Hauptstraße", "12", "10115", 11.0, 48.0)]
        assert photon_geocode.lookup("Hauptstraße", "12", "80331") is None
        photon["features"] = [_feature("Hauptstraße", "12", None, 11.0, 48.0)]
        assert photon_geocode.lookup("Hauptstraße", "12", "80331") == (48.0, 11.0)

    def test_takes_first_matching_of_several(self, app, photon):
        photon["features"] = [
            _feature("Hauptstraße", "12", "10115", 13.0, 52.0),   # falsche PLZ
            _feature("Hauptstraße", "12", "80331", 11.0, 48.0),
        ]
        assert photon_geocode.lookup("Hauptstraße", "12", "80331") == (48.0, 11.0)

    def test_missing_street_or_number_skips_the_request(self, app, photon):
        assert photon_geocode.lookup("", "12") is None
        assert photon_geocode.lookup("Hauptstraße", "") is None
        assert photon["queries"] == []

    def test_no_url_raises(self, app, monkeypatch):
        monkeypatch.setitem(app.config, "GEOCODER_PHOTON_URL", "")
        with pytest.raises(photon_geocode.PhotonError):
            photon_geocode.lookup("Hauptstraße", "12")


class TestGeocodeAddressDispatch:
    def test_de_returns_lat_lng_dict(self, app, photon):
        _set_country("DE")
        photon["features"] = [_feature("Hauptstraße", "12", "80331", 11.5754, 48.1372)]
        assert geocoding.geocode_address("Hauptstraße", "12", plz="80331", ort="München") == {
            "lat": 48.1372, "lng": 11.5754}

    def test_de_no_hit_returns_none(self, app, photon):
        _set_country("DE")
        assert geocoding.geocode_address("Hauptstraße", "12") is None

    def test_de_service_error_becomes_geocoding_error(self, app, photon):
        _set_country("DE")
        photon["error"] = "Geocoding-Dienst nicht erreichbar (timeout)."
        with pytest.raises(geocoding.GeocodingError, match="nicht erreichbar"):
            geocoding.geocode_address("Hauptstraße", "12")

    def test_de_without_url_becomes_geocoding_error(self, app, monkeypatch):
        _set_country("DE")
        monkeypatch.setitem(app.config, "GEOCODER_PHOTON_URL", "")
        with pytest.raises(geocoding.GeocodingError, match="GEOCODER_PHOTON_URL"):
            geocoding.geocode_address("Hauptstraße", "12")

    def test_at_without_index_becomes_geocoding_error(self, app, monkeypatch, tmp_path):
        monkeypatch.setitem(app.config, "BEV_INDEX_PATH", str(tmp_path / "missing.sqlite"))
        with pytest.raises(geocoding.GeocodingError, match="BEV"):
            geocoding.geocode_address("Hauptstraße", "12")


def _prop(**kw):
    kw.setdefault("object_type", "Haus")
    kw.setdefault("active", True)
    p = Property(**kw)
    db.session.add(p)
    db.session.commit()
    return p


class TestGeocodeProperties:
    def test_de_without_url_explains_manual_fallback(self, app, monkeypatch):
        _set_country("DE")
        monkeypatch.setitem(app.config, "GEOCODER_PHOTON_URL", "")
        with pytest.raises(geocoding.GeocodingError, match="manuell auf der Karte"):
            geocoding.geocode_properties()

    def test_de_sets_coordinates_and_reports_misses(self, app, photon):
        _set_country("DE")
        hit = _prop(strasse="Hauptstraße", hausnummer="12", plz="80331", ort="München")
        miss = _prop(strasse="Nirgendwo", hausnummer="1", plz="80331", ort="München")
        photon["features"] = [_feature("Hauptstraße", "12", "80331", 11.5754, 48.1372)]
        result = geocoding.geocode_properties()
        assert result["total"] == 2 and result["geocoded"] == 1
        assert result["not_found"] == [miss.label()]
        db.session.refresh(hit)
        assert (hit.lat, hit.lng) == (48.1372, 11.5754)
        assert hit.geocoded_at is not None
        db.session.refresh(miss)
        assert miss.lat is None

    def test_only_missing_skips_positioned_properties(self, app, photon):
        _set_country("DE")
        placed = _prop(strasse="Hauptstraße", hausnummer="12", plz="80331",
                       lat=1.0, lng=2.0)
        photon["features"] = [_feature("Hauptstraße", "12", "80331", 11.5754, 48.1372)]
        assert geocoding.geocode_properties(only_missing=True)["total"] == 0
        db.session.refresh(placed)
        assert (placed.lat, placed.lng) == (1.0, 2.0)
        # „Alle neu": überschreibt
        result = geocoding.geocode_properties(only_missing=False)
        assert result["geocoded"] == 1
        db.session.refresh(placed)
        assert (placed.lat, placed.lng) == (48.1372, 11.5754)

    def test_inactive_properties_are_ignored(self, app, photon):
        _set_country("DE")
        _prop(strasse="Hauptstraße", hausnummer="12", plz="80331", active=False)
        photon["features"] = [_feature("Hauptstraße", "12", "80331", 11.0, 48.0)]
        assert geocoding.geocode_properties()["total"] == 0
