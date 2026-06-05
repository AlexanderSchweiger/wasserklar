"""HTTP-Tests fuer das Technik-/Leitungsplan-Modul.

CSRF ist im Test-Modus aus (TestingConfig). Cookie-Jar-Stolperer (Werkzeug 3):
``_login`` macht vorher ``/auth/logout``.
"""
import base64
import io
import json
from datetime import date

import pytest

from app.extensions import db
from app.models import NetworkFeature, MaintenanceLog, FeaturePhoto, User
from tests.conftest import _ensure_role

# 1x1-PNG (transparent)
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@pytest.fixture
def admin(app):
    role = _ensure_role("Admin")
    u = User(username="admin", email="admin@test.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def plain_user(app):
    """User mit einer Rolle OHNE 'technik'-Recht."""
    role = _ensure_role("NurStammdaten", perms=["stammdaten"])
    u = User(username="hans", email="hans@test.test", role_id=role.id)
    u.set_password("secret")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture(autouse=True)
def active_plan(app):
    """Jeder Technik-Test braucht einen aktuellen Plan — Features haengen an
    ``plan_id`` (NOT NULL). ``current_plan()`` faellt auf den ersten aktiven Plan
    zurueck, daher reicht es, einen anzulegen."""
    from app.models import NetworkPlan
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


class TestAccess:
    def test_login_required(self, client, admin):
        client.get("/auth/logout")
        r = client.get("/technik/")
        assert r.status_code == 302
        assert "/auth/login" in r.headers["Location"]

    def test_permission_gate_redirects(self, client, plain_user):
        _login(client, "hans")
        r = client.get("/technik/", follow_redirects=True)
        assert r.status_code == 200
        assert "Kein Zugriff" in r.get_data(as_text=True)

    def test_index_loads_for_admin(self, client, admin):
        _login(client)
        r = client.get("/technik/")
        assert r.status_code == 200
        assert "Leitungsplan" in r.get_data(as_text=True)


class TestFeatureCrud:
    def test_create_point_and_geojson(self, client, admin):
        _login(client)
        r = _make_point(client, name="Hydrant 1")
        assert r.status_code == 201
        feat = r.get_json()
        assert feat["properties"]["feature_type"] == "hydrant"
        fid = feat["id"]
        assert db.session.get(NetworkFeature, fid) is not None

        r2 = client.get("/technik/features.geojson")
        assert r2.status_code == 200
        coll = r2.get_json()
        assert coll["type"] == "FeatureCollection"
        assert any(f["id"] == fid for f in coll["features"])

    def test_create_invalid_geometry_400(self, client, admin):
        _login(client)
        r = client.post("/technik/features", json={"geometry": {"type": "Polygon", "coordinates": []}})
        assert r.status_code == 400

    def test_unknown_type_defaults_to_sonstiges(self, client, admin):
        _login(client)
        r = _make_point(client, ftype="voll-erfunden")
        assert r.get_json()["properties"]["feature_type"] == "sonstiges"

    def test_update_attributes(self, client, admin):
        _login(client)
        fid = _make_point(client).get_json()["id"]
        r = client.post(f"/technik/features/{fid}", data={
            "feature_type": "schieber", "name": "S-1", "accuracy": "exakt",
            "material": "Guss (GG)", "dimension_dn": "100", "year_built": "1990",
        })
        assert r.status_code == 200
        f = db.session.get(NetworkFeature, fid)
        assert f.feature_type == "schieber"
        assert f.name == "S-1"
        assert f.accuracy == "exakt"
        assert f.dimension_dn == 100
        assert f.year_built == 1990

    def test_update_geometry_recomputes_length(self, client, admin):
        _login(client)
        r = client.post("/technik/features", json={
            "geometry": {"type": "LineString", "coordinates": [[16.0, 48.0], [16.0, 48.001]]},
            "feature_type": "versorgungsleitung",
        })
        fid = r.get_json()["id"]
        r2 = client.post(f"/technik/features/{fid}/geometry", json={
            "geometry": {"type": "LineString", "coordinates": [[16.0, 48.0], [16.0, 48.002]]},
        })
        assert r2.status_code == 200
        f = db.session.get(NetworkFeature, fid)
        assert f.length_m == pytest.approx(222.6, abs=4.0)

    def test_delete(self, client, admin):
        _login(client)
        fid = _make_point(client).get_json()["id"]
        r = client.post(f"/technik/features/{fid}/delete")
        assert r.status_code == 200
        assert db.session.get(NetworkFeature, fid) is None

    def test_panel_loads(self, client, admin):
        _login(client)
        fid = _make_point(client).get_json()["id"]
        r = client.get(f"/technik/features/{fid}")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Lagegenauigkeit" in body
        assert "Wartung" in body


class TestPrint:
    def test_print_loads(self, client, admin):
        _login(client)
        _make_point(client, name="P1")
        r = client.get("/technik/print")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Legende" in body
        assert "Elementliste" in body


class TestMaintenance:
    def test_add_with_interval_sets_next_due(self, client, admin):
        _login(client)
        fid = _make_point(client).get_json()["id"]
        r = client.post(f"/technik/features/{fid}/maintenance", data={
            "date": "2026-01-01", "kind": "spuelung", "result": "ok", "interval_months": "12",
        })
        assert r.status_code == 200
        log = MaintenanceLog.query.filter_by(feature_id=fid).one()
        assert log.kind == "spuelung"
        assert log.result == "ok"
        assert log.next_due == date(2027, 1, 1)


class TestImportExport:
    def test_export(self, client, admin):
        _login(client)
        _make_point(client, name="X")
        r = client.get("/technik/export.geojson")
        assert r.status_code == 200
        assert "geo+json" in r.headers["Content-Type"]
        assert "attachment" in r.headers["Content-Disposition"]
        assert r.get_json()["type"] == "FeatureCollection"

    def test_import_preview_then_commit(self, client, admin):
        _login(client)
        raw = json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [16, 48]},
             "properties": {"feature_type": "hydrant", "name": "Imp1"}},
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[16, 48], [16.001, 48]]},
             "properties": {"feature_type": "versorgungsleitung"}},
        ]})
        # Schritt 1: Vorschau
        r = client.post(
            "/technik/import",
            data={"file": (io.BytesIO(raw.encode("utf-8")), "plan.geojson")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200
        assert "Vorschau" in r.get_data(as_text=True)
        assert NetworkFeature.query.count() == 0  # noch nichts geschrieben

        # Schritt 2: Commit
        r2 = client.post("/technik/import", data={"confirm": "1", "geojson": raw}, follow_redirects=False)
        assert r2.status_code == 302
        assert NetworkFeature.query.count() == 2


class TestPhotos:
    def test_upload_and_serve(self, client, admin, tmp_path, monkeypatch):
        # Foto-Ordner in einen tmp-Pfad umbiegen (PDF_DIR-Geschwister).
        monkeypatch.setitem(client.application.config, "PDF_DIR", str(tmp_path / "pdfs"))
        _login(client)
        fid = _make_point(client).get_json()["id"]
        r = client.post(
            f"/technik/features/{fid}/photos",
            data={"photo": (io.BytesIO(_PNG), "hydrant.png")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200
        photo = FeaturePhoto.query.filter_by(feature_id=fid).one()
        assert (photo.content_type or "").startswith("image/")

        r2 = client.get(f"/technik/photos/{photo.id}")
        assert r2.status_code == 200
        assert r2.data == _PNG

    def test_upload_rejects_non_image(self, client, admin, tmp_path, monkeypatch):
        monkeypatch.setitem(client.application.config, "PDF_DIR", str(tmp_path / "pdfs"))
        _login(client)
        fid = _make_point(client).get_json()["id"]
        r = client.post(
            f"/technik/features/{fid}/photos",
            data={"photo": (io.BytesIO(b"not an image"), "evil.txt")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200
        assert FeaturePhoto.query.filter_by(feature_id=fid).count() == 0


class TestPlans:
    def test_list_loads(self, client, admin):
        _login(client)
        r = client.get("/technik/plans")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Testplan" in body
        assert "Neuer Plan" in body

    def test_create_and_edit(self, client, admin):
        from app.models import NetworkPlan
        _login(client)
        r = client.post("/technik/plans", data={
            "name": "Ausbau 2026", "status": "entwurf", "maintenance_enabled": "1",
        }, follow_redirects=True)
        assert r.status_code == 200
        p = NetworkPlan.query.filter_by(name="Ausbau 2026").one()
        assert p.status == "entwurf" and p.maintenance_enabled is True
        pid = p.id

        # Bearbeiten: umbenennen, aktiv setzen, Wartung aus (Checkbox fehlt -> False).
        client.post(f"/technik/plans/{pid}", data={
            "name": "Ausbau 2026/27", "status": "aktiv",
        }, follow_redirects=True)
        p2 = db.session.get(NetworkPlan, pid)
        assert p2.name == "Ausbau 2026/27"
        assert p2.status == "aktiv"
        assert p2.maintenance_enabled is False

    def test_copy_and_merge(self, client, admin, active_plan):
        from app.models import NetworkPlan, NetworkFeature
        src_id = active_plan.id
        _login(client)
        fid = _make_point(client, name="A").get_json()["id"]

        client.post(f"/technik/plans/{src_id}/copy", follow_redirects=True)
        dup = NetworkPlan.query.filter(NetworkPlan.source_plan_id == src_id).one()
        assert dup.maintenance_enabled is False
        assert len(dup.features) == 1

        # In der Kopie aendern, dann zurueck in den Quellplan uebertragen.
        dup.features[0].name = "A-neu"
        db.session.commit()
        client.post(f"/technik/plans/{dup.id}/merge", follow_redirects=True)
        assert db.session.get(NetworkFeature, fid).name == "A-neu"

    def test_delete(self, client, admin):
        from app.models import NetworkPlan
        _login(client)
        p = NetworkPlan(name="Weg", status="entwurf")
        db.session.add(p)
        db.session.commit()
        pid = p.id
        client.post(f"/technik/plans/{pid}/delete", follow_redirects=True)
        assert db.session.get(NetworkPlan, pid) is None
