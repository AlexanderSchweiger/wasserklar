"""HTTP-Tests fuer Wasserproben/TWV-Beprobung: Befund-Erfassung (Modal +
Karten-Panel-inline), Gating auf feature_type='probenahme', Wasserqualitäts-Seite
inkl. Empty-States, CSV-Export, Grenzwert-Override, Bericht, Permission-Gate.

CSRF im Test aus. Cookie-Jar-Stolperer: ``_login`` macht vorher ``/auth/logout``.
"""
import pytest

from app.extensions import db
from app.models import (
    AppSetting, NetworkPlan, NetworkFeature, WaterSample, LabResult, User,
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


@pytest.fixture
def viewer(app):
    role = _ensure_role("Viewer", perms=["stammdaten"])  # kein 'network'
    u = User(username="viewer", email="viewer@test.test", role_id=role.id)
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


def _login(client, username="admin"):
    client.get("/auth/logout")
    return client.post("/auth/login", data={"username": username, "password": "secret"})


def _make_point(client, ftype="probenahme", lng=16.37, lat=48.21, **props):
    body = {"geometry": {"type": "Point", "coordinates": [lng, lat]}, "feature_type": ftype}
    body.update(props)
    return client.post("/network/features", json=body)


_MOD = {"X-From-Modal": "1"}


class TestSampleModal:
    def test_add_creates_sample_and_results(self, client, admin):
        _login(client)
        fid = _make_point(client, name="PN1").get_json()["id"]
        r = client.post(
            f"/network/features/{fid}/samples",
            data={"sample_date": "2026-06-01", "lab_name": "Labor X",
                  "value__nitrat": "12,5", "value__e_coli": "0"},
            headers=_MOD,
        )
        assert r.status_code == 200
        assert "sampleSaved" in r.headers.get("HX-Trigger", "")
        s = WaterSample.query.filter_by(feature_id=fid).one()
        assert s.lab_name == "Labor X"
        results = {x.parameter_key: x for x in s.results}
        assert str(results["nitrat"].value_num) == "12.5000"   # dt. Komma akzeptiert
        assert results["nitrat"].status == "ok"
        assert results["nitrat"].unit == "mg/l"                # Snapshot
        assert results["nitrat"].limit_text == "50 mg/l"       # Snapshot
        assert results["e_coli"].status == "ok"

    def test_alarm_status_on_exceedance(self, client, admin):
        _login(client)
        fid = _make_point(client, name="PN2").get_json()["id"]
        client.post(f"/network/features/{fid}/samples",
                    data={"value__nitrat": "60"}, headers=_MOD)
        s = WaterSample.query.filter_by(feature_id=fid).one()
        assert s.results[0].status == "alarm"
        assert s.overall_status() == "alarm"

    def test_non_numeric_value_is_text_unknown(self, client, admin):
        _login(client)
        fid = _make_point(client, name="PN3").get_json()["id"]
        client.post(f"/network/features/{fid}/samples",
                    data={"value__e_coli": "n.n."}, headers=_MOD)
        r = LabResult.query.join(WaterSample).filter(WaterSample.feature_id == fid).one()
        assert r.value_num is None
        assert r.value_text == "n.n."
        assert r.status == "unknown"

    def test_empty_values_create_nothing(self, client, admin):
        _login(client)
        fid = _make_point(client, name="PN4").get_json()["id"]
        r = client.post(f"/network/features/{fid}/samples",
                        data={"sample_date": "2026-06-01"}, headers=_MOD)
        assert r.status_code == 200
        assert "sampleSaved" not in r.headers.get("HX-Trigger", "")
        assert WaterSample.query.filter_by(feature_id=fid).count() == 0
        assert "mindestens einen Laborwert" in r.get_data(as_text=True)

    def test_non_probenahme_is_404(self, client, admin):
        _login(client)
        fid = _make_point(client, ftype="hydrant", name="H1").get_json()["id"]
        r = client.post(f"/network/features/{fid}/samples",
                        data={"value__nitrat": "10"}, headers=_MOD)
        assert r.status_code == 404
        assert WaterSample.query.filter_by(feature_id=fid).count() == 0

    def test_delete_cascades(self, client, admin):
        _login(client)
        fid = _make_point(client, name="PN5").get_json()["id"]
        client.post(f"/network/features/{fid}/samples",
                    data={"value__nitrat": "10"}, headers=_MOD)
        s = WaterSample.query.filter_by(feature_id=fid).one()
        r = client.post(f"/network/samples/{s.id}/delete", headers=_MOD)
        assert r.status_code == 200
        assert "sampleSaved" in r.headers.get("HX-Trigger", "")
        assert WaterSample.query.filter_by(feature_id=fid).count() == 0
        assert LabResult.query.count() == 0


class TestPanelSection:
    def test_probenahme_panel_has_sample_section(self, client, admin):
        _login(client)
        fid = _make_point(client, name="PN6").get_json()["id"]
        body = client.get(f"/network/features/{fid}").get_data(as_text=True)
        assert "Wasserproben" in body
        assert 'name="value__nitrat"' in body

    def test_non_probenahme_panel_has_no_sample_section(self, client, admin):
        _login(client)
        fid = _make_point(client, ftype="schieber", name="S1").get_json()["id"]
        body = client.get(f"/network/features/{fid}").get_data(as_text=True)
        assert 'name="value__nitrat"' not in body

    def test_panel_add_inline_returns_full_panel(self, client, admin):
        _login(client)
        fid = _make_point(client, name="PN7").get_json()["id"]
        r = client.post(f"/network/features/{fid}/samples", data={"value__nitrat": "11"})
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'name="feature_type"' in body   # volles Panel (Stammdaten-Form)
        assert WaterSample.query.filter_by(feature_id=fid).count() == 1


class TestWaterQualityPage:
    def test_empty_when_no_probenahme(self, client, admin):
        _login(client)
        r = client.get("/network/water-quality")
        assert r.status_code == 200
        assert "keine" in r.get_data(as_text=True).lower()

    def test_page_with_stelle_and_sample(self, client, admin):
        _login(client)
        fid = _make_point(client, name="PNQ").get_json()["id"]
        client.post(f"/network/features/{fid}/samples",
                    data={"sample_date": "2026-06-01", "value__nitrat": "55"}, headers=_MOD)
        body = client.get("/network/water-quality").get_data(as_text=True)
        assert "wqChart" in body
        assert "PNQ" in body
        assert "55" in body                  # Messwert in der Serien-JSON


class TestSamplesOverview:
    def test_lists_all_samples_with_links(self, client, admin):
        _login(client)
        fid = _make_point(client, name="PNH").get_json()["id"]
        # zwei Befunde an verschiedenen Daten
        client.post(f"/network/features/{fid}/samples",
                    data={"sample_date": "2025-03-01", "value__nitrat": "20"}, headers=_MOD)
        client.post(f"/network/features/{fid}/samples",
                    data={"sample_date": "2025-06-01", "value__nitrat": "60"}, headers=_MOD)
        r = client.get(f"/network/features/{fid}/water-samples")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        # Beide historischen Befunde gelistet (Datum) + Link zum Einzelbefund
        assert "01.03.2025" in body
        assert "01.06.2025" in body
        ids = [s.id for s in WaterSample.query.filter_by(feature_id=fid).all()]
        for sid in ids:
            assert f"/network/samples/{sid}" in body
        assert "wqChart" in body                      # Trend-Diagramm vorhanden

    def test_non_probenahme_is_404(self, client, admin):
        _login(client)
        fid = _make_point(client, ftype="hydrant", name="HX").get_json()["id"]
        assert client.get(f"/network/features/{fid}/water-samples").status_code == 404

    def test_empty_state(self, client, admin):
        _login(client)
        fid = _make_point(client, name="PNE").get_json()["id"]
        r = client.get(f"/network/features/{fid}/water-samples")
        assert r.status_code == 200
        assert "Noch keine Befunde" in r.get_data(as_text=True)


class TestCsvExport:
    def test_csv_contains_results(self, client, admin):
        _login(client)
        fid = _make_point(client, name="PNC").get_json()["id"]
        client.post(f"/network/features/{fid}/samples",
                    data={"sample_date": "2026-06-01", "value__nitrat": "12,5"}, headers=_MOD)
        r = client.get("/network/water-quality/export.csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers["Content-Type"]
        text = r.get_data(as_text=True)
        assert "Probenahmestelle" in text     # Header
        assert "Nitrat" in text
        assert "12,5" in text                  # dt. Dezimalkomma


class TestLimitsOverride:
    def test_post_sets_override_and_reflows(self, client, admin):
        _login(client)
        client.post("/network/water-quality/limits", data={"limit__nitrat": "40"})
        assert AppSetting.get("water_quality.nitrat.limit") == "40"
        # Neuer Befund mit 45 mg/l ist jetzt eine Überschreitung
        fid = _make_point(client, name="PNL").get_json()["id"]
        client.post(f"/network/features/{fid}/samples",
                    data={"value__nitrat": "45"}, headers=_MOD)
        s = WaterSample.query.filter_by(feature_id=fid).one()
        assert s.results[0].status == "alarm"
        assert s.results[0].limit_text == "40 mg/l"

    def test_empty_override_deletes(self, client, admin):
        _login(client)
        AppSetting.set("water_quality.nitrat.limit", "40")
        db.session.commit()
        client.post("/network/water-quality/limits", data={"limit__nitrat": ""})
        assert AppSetting.get("water_quality.nitrat.limit") is None


class TestReport:
    def test_print_view_ok(self, client, admin):
        _login(client)
        r = client.get("/network/water-quality/print")
        assert r.status_code == 200
        assert "Untersuchungsbericht" in r.get_data(as_text=True)

    def test_report_pdf_ok_or_fallback(self, client, admin):
        _login(client)
        r = client.get("/network/water-quality/report.pdf")
        # Ohne WeasyPrint (requirements-dev) -> Redirect auf Druckansicht.
        assert r.status_code in (200, 302)


class TestPermissionGate:
    def test_viewer_without_network_redirected(self, client, viewer):
        _login(client, "viewer")
        r = client.get("/network/water-quality")
        assert r.status_code == 302
