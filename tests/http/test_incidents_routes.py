"""HTTP-Tests fuer das Stoerungs-/Rohrbruch-Journal (Blueprint ``incidents``).

Deckt ab: Permission-Gate, Modal-CRUD (204 + HX-Trigger), status<->resolved_at-
Kopplung, Row-Swap-Fragment, GeoJSON-Endpoint + Geometrie, CSV-Export (BOM/Format),
PDF-Fallback ohne WeasyPrint und Loeschen inkl. Fotodateien.
"""
import json
import os

import pytest

from app.extensions import db
from app.models import User, Incident, IncidentPhoto
from tests.conftest import _ensure_role


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="a@a.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def viewer(app):
    """User ohne ``incidents``-Recht (nur stammdaten)."""
    role = _ensure_role("Viewer", perms=["stammdaten"])
    u = User(username="viewer", email="v@v.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username="admin"):
    return client.post("/auth/login", data={"username": username, "password": "secret"})


def _modal_headers():
    return {"X-From-Modal": "1"}


class TestAccess:
    def test_index_requires_login(self, client):
        client.get("/auth/logout")
        resp = client.get("/incidents/")
        assert resp.status_code == 302
        assert "/auth/login" in resp.headers["Location"]

    def test_index_ok_for_admin(self, client, admin):
        _login(client)
        resp = client.get("/incidents/")
        assert resp.status_code == 200
        assert "Störungsjournal" in resp.get_data(as_text=True)

    def test_permission_gate_redirects(self, client, viewer):
        client.get("/auth/logout")
        _login(client, "viewer")
        resp = client.get("/incidents/")
        assert resp.status_code == 302
        assert "/auth/login" not in resp.headers["Location"]  # -> dashboard, nicht login


class TestModalCrud:
    def test_create_redirects_to_detail(self, client, admin):
        _login(client)
        resp = client.post("/incidents/new",
                           data={"title": "Rohrbruch Test", "incident_type": "rohrbruch",
                                 "severity": "hoch", "status": "offen", "detected_at": "2026-03-01"},
                           headers=_modal_headers())
        inc = Incident.query.one()
        # Neu angelegte Störung wird sofort geöffnet (HX-Redirect auf Detail).
        assert resp.headers.get("HX-Redirect") == f"/incidents/{inc.id}"
        assert inc.title == "Rohrbruch Test"
        assert inc.severity == "hoch"

    def test_create_empty_title_returns_form_with_error(self, client, admin):
        _login(client)
        resp = client.post("/incidents/new", data={"title": ""}, headers=_modal_headers())
        assert resp.status_code == 200
        assert "Titel" in resp.get_data(as_text=True)
        assert Incident.query.count() == 0

    def test_invalid_type_falls_back_to_default(self, client, admin):
        _login(client)
        client.post("/incidents/new",
                    data={"title": "X", "incident_type": "bogus"}, headers=_modal_headers())
        assert Incident.query.one().incident_type == Incident.TYPE_ROHRBRUCH

    def test_status_resolved_sets_resolved_at(self, client, admin):
        _login(client)
        client.post("/incidents/new",
                    data={"title": "X", "detected_at": "2026-03-01", "status": "offen"},
                    headers=_modal_headers())
        inc = Incident.query.one()
        assert inc.resolved_at is None
        # -> behoben setzt resolved_at automatisch
        client.post(f"/incidents/{inc.id}/edit",
                    data={"title": "X", "detected_at": "2026-03-01", "status": "behoben"},
                    headers=_modal_headers())
        db.session.refresh(inc)
        assert inc.resolved_at is not None
        # zurueck auf offen leert es wieder
        client.post(f"/incidents/{inc.id}/edit",
                    data={"title": "X", "detected_at": "2026-03-01", "status": "offen"},
                    headers=_modal_headers())
        db.session.refresh(inc)
        assert inc.resolved_at is None

    def test_row_fragment(self, client, admin):
        _login(client)
        client.post("/incidents/new", data={"title": "Zeile"}, headers=_modal_headers())
        inc = Incident.query.one()
        resp = client.get(f"/incidents/{inc.id}/row")
        assert resp.status_code == 200
        assert f'incident-row-{inc.id}' in resp.get_data(as_text=True)


class TestMapAndGeometry:
    def test_geometry_sets_latlng(self, client, admin):
        _login(client)
        client.post("/incidents/new", data={"title": "Pin"}, headers=_modal_headers())
        inc = Incident.query.one()
        resp = client.post(f"/incidents/{inc.id}/geometry",
                           json={"geometry": {"type": "Point", "coordinates": [14.1, 47.5]}})
        assert resp.status_code == 200
        db.session.refresh(inc)
        assert inc.lat == 47.5 and inc.lng == 14.1

    def test_geometry_invalid(self, client, admin):
        _login(client)
        client.post("/incidents/new", data={"title": "Pin"}, headers=_modal_headers())
        inc = Incident.query.one()
        resp = client.post(f"/incidents/{inc.id}/geometry",
                           json={"geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}})
        assert resp.status_code == 400

    def test_map_geojson_only_located(self, client, admin):
        _login(client)
        client.post("/incidents/new", data={"title": "ohne Pin", "detected_at": "2026-01-01"},
                    headers=_modal_headers())
        client.post("/incidents/new", data={"title": "mit Pin", "detected_at": "2025-01-01"},
                    headers=_modal_headers())
        located = Incident.query.filter_by(title="mit Pin").one()
        client.post(f"/incidents/{located.id}/geometry",
                    json={"geometry": {"type": "Point", "coordinates": [14.1, 47.5]}})
        fc = client.get("/incidents/map.geojson").get_json()
        assert fc["type"] == "FeatureCollection"
        assert len(fc["features"]) == 1
        # Jahr-Filter auf detected_at
        fc2025 = client.get("/incidents/map.geojson?year=2025").get_json()
        assert len(fc2025["features"]) == 1
        fc2026 = client.get("/incidents/map.geojson?year=2026").get_json()
        assert len(fc2026["features"]) == 0


class TestExport:
    def test_csv_has_bom_and_german_format(self, client, admin):
        _login(client)
        client.post("/incidents/new",
                    data={"title": "CSV", "detected_at": "2026-03-01", "cost": "850,50"},
                    headers=_modal_headers())
        resp = client.get("/incidents/export.csv")
        assert resp.status_code == 200
        assert resp.data.startswith(b"\xef\xbb\xbf")  # UTF-8-BOM
        text = resp.data.decode("utf-8-sig")
        assert "Wasserverlust (m³)" in text
        assert "01.03.2026" in text
        assert "850,50" in text

    def test_pdf_without_weasyprint_redirects(self, client, admin):
        _login(client)
        resp = client.get("/incidents/report.pdf")
        # ohne WeasyPrint (dev): Redirect auf Druckansicht; mit: 200 PDF.
        assert resp.status_code in (200, 302)


class TestDelete:
    def test_delete_removes_record_and_photo_files(self, client, admin, app):
        _login(client)
        client.post("/incidents/new", data={"title": "Del"}, headers=_modal_headers())
        inc = Incident.query.one()
        # Foto-Datei + Record anlegen
        from app.incidents.services import incident_upload_dir
        folder = incident_upload_dir()
        fname = "smoke_test.jpg"
        path = os.path.join(folder, fname)
        with open(path, "wb") as fh:
            fh.write(b"x")
        db.session.add(IncidentPhoto(incident_id=inc.id, filename=fname))
        db.session.commit()
        assert os.path.exists(path)
        client.post(f"/incidents/{inc.id}/delete", data={})
        assert Incident.query.count() == 0
        assert IncidentPhoto.query.count() == 0
        assert not os.path.exists(path)
