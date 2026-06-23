"""HTTP-Tests fuer Quellschüttung: Modal-Erfassung (Elementliste/Monitoring),
inline Karten-Panel-Sektion, Gating auf feature_type='quelle', Monitoring-Seite
inkl. Empty-States.

CSRF im Test aus. Cookie-Jar-Stolperer: ``_login`` macht vorher ``/auth/logout``.
"""
import pytest

from app.extensions import db
from app.models import NetworkPlan, NetworkFeature, SpringYield, User
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


def _login(client):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": "admin", "password": "secret"})


def _make_point(client, ftype="quelle", lng=16.37, lat=48.21, **props):
    body = {"geometry": {"type": "Point", "coordinates": [lng, lat]}, "feature_type": ftype}
    body.update(props)
    return client.post("/network/features", json=body)


_MOD = {"X-From-Modal": "1"}


class TestYieldModal:
    def test_add_creates_and_triggers(self, client, admin):
        _login(client)
        fid = _make_point(client, name="Q1").get_json()["id"]
        r = client.post(f"/network/features/{fid}/yields",
                        data={"measurement_date": "2026-06-01", "flow_rate_lps": "1,5"},
                        headers=_MOD)
        assert r.status_code == 200
        assert "yieldSaved" in r.headers.get("HX-Trigger", "")
        y = SpringYield.query.filter_by(feature_id=fid).one()
        assert str(y.flow_rate_lps) == "1.500"          # deutsches Komma akzeptiert
        assert "1,50 l/s" in r.get_data(as_text=True)   # Liste im Modal-Body aktualisiert

    def test_invalid_value_no_record(self, client, admin):
        _login(client)
        fid = _make_point(client, name="Q2").get_json()["id"]
        r = client.post(f"/network/features/{fid}/yields",
                        data={"flow_rate_lps": "abc"}, headers=_MOD)
        assert r.status_code == 200
        assert "yieldSaved" not in r.headers.get("HX-Trigger", "")
        assert SpringYield.query.filter_by(feature_id=fid).count() == 0
        assert "gültige Schüttung" in r.get_data(as_text=True)

    def test_negative_value_rejected(self, client, admin):
        _login(client)
        fid = _make_point(client, name="Q2b").get_json()["id"]
        client.post(f"/network/features/{fid}/yields", data={"flow_rate_lps": "-3"}, headers=_MOD)
        assert SpringYield.query.filter_by(feature_id=fid).count() == 0

    def test_non_quelle_is_404(self, client, admin):
        _login(client)
        fid = _make_point(client, ftype="hydrant", name="H1").get_json()["id"]
        r = client.post(f"/network/features/{fid}/yields", data={"flow_rate_lps": "1.0"}, headers=_MOD)
        assert r.status_code == 404
        assert SpringYield.query.filter_by(feature_id=fid).count() == 0

    def test_delete(self, client, admin):
        _login(client)
        fid = _make_point(client, name="Q3").get_json()["id"]
        client.post(f"/network/features/{fid}/yields", data={"flow_rate_lps": "2.0"}, headers=_MOD)
        y = SpringYield.query.filter_by(feature_id=fid).one()
        r = client.post(f"/network/yields/{y.id}/delete", headers=_MOD)
        assert r.status_code == 200
        assert "yieldSaved" in r.headers.get("HX-Trigger", "")
        assert SpringYield.query.filter_by(feature_id=fid).count() == 0


class TestPanelSection:
    def test_quelle_panel_has_yield_section(self, client, admin):
        _login(client)
        fid = _make_point(client, name="Q4").get_json()["id"]
        body = client.get(f"/network/features/{fid}").get_data(as_text=True)
        assert "Quellschüttung" in body
        assert 'name="flow_rate_lps"' in body

    def test_non_quelle_panel_has_no_yield_section(self, client, admin):
        _login(client)
        fid = _make_point(client, ftype="schieber", name="S1").get_json()["id"]
        body = client.get(f"/network/features/{fid}").get_data(as_text=True)
        assert 'name="flow_rate_lps"' not in body

    def test_panel_add_inline_returns_full_panel(self, client, admin):
        """Ohne X-From-Modal liefert yield_add das volle Karten-Panel zurueck."""
        _login(client)
        fid = _make_point(client, name="Q5").get_json()["id"]
        r = client.post(f"/network/features/{fid}/yields", data={"flow_rate_lps": "0.8"})
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'name="feature_type"' in body     # volles Panel (Stammdaten-Form)
        assert "0,80 l/s" in body
        assert SpringYield.query.filter_by(feature_id=fid).count() == 1


class TestMonitoring:
    def test_empty_when_no_quelle(self, client, admin):
        _login(client)
        r = client.get("/network/monitoring")
        assert r.status_code == 200
        assert "keine" in r.get_data(as_text=True).lower()   # „keine Quelle erfasst"

    def test_chart_with_quelle_and_reading(self, client, admin):
        _login(client)
        fid = _make_point(client, name="Q6").get_json()["id"]
        client.post(f"/network/features/{fid}/yields",
                    data={"measurement_date": "2026-06-01", "flow_rate_lps": "1.25"}, headers=_MOD)
        body = client.get("/network/monitoring").get_data(as_text=True)
        assert "yieldChart" in body          # Chart-Canvas vorhanden
        assert "Q6" in body                  # Quelle gelistet
        assert "1.25" in body                # Messwert in der Serien-JSON
